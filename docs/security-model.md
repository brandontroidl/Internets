# Security model

Maintainer's manual for the security-relevant subsystems of Internets v4.0.0. Every
claim below is grounded in the source as of this writing; file:line references are for
navigation. Read the code alongside this.

The bot's security posture is built from independent layers, each with a single
canonical implementation: the admin/auth boundary (`internets.py` + `admin_cmds.py`),
the two-tier secret store (`secret_store.py`), the SSRF guard (`modules/_netsafe.py` +
`modules/base.py:resolve_public`), outbound size caps (`modules/base.py:fetch_json`),
state-file integrity (`store.py`), the tamper-evident audit log (`audit_log.py`), the
metrics bind guard (`metrics.py`), and the uniform IRC-injection sanitizer
(`modules/base.py:strip_ctrl`). None of these depends on another being correct; treat
each as its own containment boundary.

---

## 1. Admin / auth boundary

The authorization decision is `IRCBot.is_admin(nick)` (`internets.py:347`). It is the
single privileged-boundary check; `admin_cmds.py:_require_admin` (line 76) just wraps it
with a user-facing "auth first" message. Every privileged `cmd_*` calls `_require_admin`
first.

### Auth state

Two dicts, both guarded by one lock `self._auth_lock` (`internets.py:225`):

- `self._authed: dict[str, str]` (line 227) - lowercased nick -> the hostmask bound at
  auth time.
- `self._nick_hosts: dict[str, str]` (line 271) - lowercased nick -> the most recently
  observed hostmask for that nick.

The lock guards BOTH dicts together (the comment at line 225 says so) because `is_admin`
reads both and must see a consistent pair. `_nick_hosts` is updated on every inbound
PRIVMSG (`internets.py:1075-1076`), and on JOIN/NICK/CHGHOST/ACCOUNT events.

### is_admin re-checks the live binding, fail-closed

`is_admin` does NOT just test membership in `_authed`. It re-derives authorization from
the CURRENT hostmask on every call (`internets.py:347-367`):

```
if k not in self._authed:                       return False
stored  = self._authed[k]                        # hostmask bound at auth
current = self._nick_hosts.get(k)                # live hostmask
if current and stored != "unknown" and current == stored:   return True
if stored == "unknown" or (current and current != stored):
    del self._authed[k]                          # revoke
    log.warning("Auth revoked ...")
return False
```

Grant happens ONLY when the live hostmask is known and equals the bound one. Three deny
paths, all fail-closed:

- nick not in `_authed` -> deny.
- live hostmask missing/None, or the bound value is the `"unknown"` sentinel -> deny
  (and the `"unknown"` sentinel case actively deletes the entry).
- live hostmask present but differs from bound -> deny AND delete the session.

The comment at lines 357-360 records the prior bug this fixed: an `"unknown"`/missing
value used to skip the comparison and return True, so a nick-only admin session
re-created during the auth TOCTOU outlived the admin's disconnect and any nick-grabber
inherited it. Do not reintroduce a "missing hostmask -> allow" path.

### cmd_auth requires a verified hostmask to bind

`admin_cmds.py:cmd_auth` (line 98), PM-only, password checked with
`verify_password` in a worker thread (line 148). On a correct password it still refuses
to create a session unless a concrete hostmask is currently known
(`admin_cmds.py:171-185`):

```
if ok:
    hostmask = self._nick_hosts.get(k)
    if not hostmask or hostmask == "unknown":
        # fail closed: never persist a binding we cannot later verify
        ... "can't confirm your hostmask right now - re-send the command."
        return
    with self._auth_lock:
        self._auth_fails.pop(k, None)
        self._authed[k] = hostmask
```

Why: the admin can quit during the `verify_password` await (which drops their
`_nick_hosts` entry, see below). Persisting the `"unknown"` sentinel would grant a
nick-only session that `is_admin` could not later tie to a hostmask - exactly the hole
`is_admin`'s fail-closed branch defends. The two checks are belt-and-suspenders: bind
only a real hostmask, and re-verify it on every use.

### Brute-force lockout

Constants on the class: `_AUTH_MAX_FAILS = 5`, `_AUTH_LOCKOUT = 300` (seconds),
`_AUTH_CLEANUP_THRESHOLD = 50` (`internets.py:157-159`). Failures tracked in
`self._auth_fails: dict[str, tuple[int, float]]` = nick -> (fail_count, last_ts).

- After 5 failures within the window, attempts are refused for the remaining lockout
  time (`admin_cmds.py:135-145`). The lockout is a SLIDING window: a refused attempt
  rewrites `last_t = now` (line 140), so trickling one attempt per window cannot bypass
  the rate limit.
- The fail counter is re-read INSIDE the lock after the verify await before incrementing
  (`admin_cmds.py:164-168` for the unexpected-backend-error path, `191-197` for the
  wrong-password path). The count was snapshotted before the await; a concurrent attempt
  could have bumped it, so re-reading avoids under-counting failures.
- `_auth_fails` is opportunistically pruned when it exceeds `_AUTH_CLEANUP_THRESHOLD`
  (line 127-131), bounding memory against a flood of distinct attacker nicks.

### Password never leaks

- Password value is never logged; only presence/length. The dispatch log line redacts
  `auth`/`deauth` args entirely: `log_arg = "[REDACTED]" if cmd in ("auth","deauth")`
  (`internets.py:1096`).
- `verify_password` exceptions are handled in two tiers (`admin_cmds.py:149-170`): a
  `ValueError` (known hashpw config error, no password content) is logged with its
  message; ANY other backend exception is logged as `type(e).__name__` only, because
  argon2/bcrypt/scrypt backends occasionally echo input or hash fragments in exception
  text. The unexpected-exception path also counts as a failed attempt.
- The audit record for a successful auth passes `None` as args (`admin_cmds.py:189`),
  never the password or a derivative.

### A session is a routing handle, not an authz boundary

The session keys on nick, but nick is a routing handle that the network can reassign.
Authorization is therefore re-bound to the hostmask on every `is_admin` call, and the
session is destroyed on any identity-changing event. Handled in `_handle_membership`
(`internets.py:984`):

- QUIT (`internets.py:1033-1046`): drop the cached hostmask AND pop any `_authed` entry -
  a reconnector reusing the nick must re-auth.
- NICK (`internets.py:1047-1066`): the session is DROPPED, not migrated to the new nick.
  The comment at 1056-1059 records why: migrating let a malicious server or a
  nick-takeover launder an authed session onto an attacker-chosen nick.
- CHGHOST / ACCOUNT (`internets.py:985-1004`): refresh the cached hostmask so the next
  `is_admin` comparison is against the current value (a changed host then revokes).
- Global drops: `_authed.clear()` on reconnect/disconnect paths
  (`internets.py:1249-1253`, `1327-1329`; `admin_cmds.py:470-472`).

Concurrency note: `is_admin` is called from `asyncio.to_thread` workers (e.g.
`flood_limited` -> `is_admin`, `internets.py:377`), so the lock is load-bearing for
correctness under free-threaded / GIL-free Python, not decorative.

---

## 2. Two-tier secret store (`secret_store.py`)

Outbound credentials must be REVERSIBLE - the bot sends them on the wire - so this is
encryption-at-rest / file-perm protection, NOT hashing. The module docstring
(lines 1-33) is explicit: hashing would break authentication. Do not "harden" a secret
by hashing it.

### Lookup order, first hit wins (`get`, line 180)

1. Env var `INTERNETS_<NAME_UPPER>` if set and non-placeholder.
2. `config.ini` `[secrets]` section, file mode strictly `0o600`.
3. Empty-string default.

`SECRETS_FILE` is `config.ini` resolved at import (line 52). config.ini is gitignored and
holds both runtime config and `[secrets]`. `config.ini.example` is the committed
credential-free template.

### Placeholder/blank filtering applies to BOTH tiers

`_PLACEHOLDERS` (lines 135-145) is a frozenset of dummy strings (`""`, `changeme`,
`your-key-here`, `todo`, `example`, `test`, ...). Filtering happens identically on the
env tier and the file tier:

- Env (lines 188-193): value is `.strip()`ed, then rejected if empty or
  `.lower() in _PLACEHOLDERS`. A whitespace or template-placeholder env export cannot
  pass as a real secret.
- File (lines 204-206): same strip + placeholder check after `parser.get`.

So a half-configured install (key left at its example value, in either tier) behaves as
"unset" - it never leaks a placeholder into an outbound request. The same filter is
mirrored in `modules/base.py:cred` (the `_PLACEHOLDER_MARKERS` substring check, lines
111-146), which is the helper modules actually call to fetch a key with a config.ini
fallback for pre-2.4.0 upgrades.

### Fail-closed file perms (`perms_ok`, line 161)

`get` reads `[secrets]` only when `perms_ok` returns true, i.e. mode is exactly `0o600`
(POSIX). A group- or world-readable config.ini causes `get` to log `REFUSING to read`
and return the default (lines 196-199) - fail closed. On Windows POSIX modes are
advisory, so `perms_ok` returns true and relies on filesystem ACLs (lines 169-171).

`delete` and `_write_file_secret` raise `PermissionError` (NOT swallowed) on loose perms
(lines 233-246, 352-355). Rationale at lines 237-241: a failed delete reported as
"not found" would make an operator believe a leaked credential was already gone.

### KNOWN_SECRETS and migration

`KNOWN_SECRETS` (lines 57-84) is the canonical list of every key the bot treats as
sensitive; adding a name here pulls it into the migrate/list/status sweeps with no other
code change. `CONFIG_LOCATIONS` (lines 88-129) maps each canonical name to its legacy
`(section, key)` in config.ini, driving `migrate` (line 462), which moves plaintext from
non-`[secrets]` sections into `[secrets]` and scrubs the source. `[secrets]` itself is
exempt from scrubbing (lines 449-451) because, when source and destination are the same
file (the default), blanking it would immediately undo the migration. `migrate` prints a
mandatory ROTATE-EVERYTHING banner (lines 654-664): the values were in git history.

### Write safety

- `set_value` rejects any value containing CR or LF (lines 227-228): the file backend
  writes `name = value` as one line, so an embedded newline would inject a fake
  section/key into config.ini.
- All writes go through `_atomic_write_text` (line 296): tmp file created with `0o600`
  from `os.open` (no world-readable window), then `os.replace`. The set/delete editors
  operate as targeted text edits on the `[secrets]` section, preserving comments and
  other sections byte-for-byte (configparser's `write()` strips comments, hence the
  manual edit).
- Exception text is logged as type only via `_safe_exc` (line 150): configparser
  includes the offending line (a partial secret) in its error messages.

### CLI never prints values

`python -m secret_store get` prints `(set, N chars, backend=...)` only (lines 547-567);
`list` prints the backend per key, never the value (lines 520-544). There is no flag to
print a secret. Legitimate extraction is the explicit
`python -c "import secret_store; print(secret_store.get('NAME'))"`. The explicit
equality branches in `_cmd_list`/`migrate` (lines 528-543, 479-482) exist to break
CodeQL's `py/clear-text-logging-sensitive-data` taint propagation; that is deliberate,
do not "simplify" them back into a tainted flow.

### Keyring removed in 3.0.0

OS keyring support was dropped (docstring lines 13-18): the bot targets headless
deployments where `keyring` has no usable backend, and it pulled ~10 transitive deps
(jeepney, secretstorage, jaraco-*, ...). The `0o600` file backend is the only store;
`set_value` always returns the label `"file"` (lines 214-230), kept only so old
callers/tests still work.

---

## 3. SSRF / netsafe layer (`modules/_netsafe.py`)

Used by any module that fetches a user-influenceable URL or resolves a user-supplied
host: `probe.py`, `scinews.py` (article reader), and `ipintel.py`. There are two
related guards - the streaming fetch (`_netsafe.py`) and the resolve-only validator
(`modules/base.py:resolve_public`, line 73).

### Blocked address ranges (`ip_is_blocked`, line 46)

Refuses private, loopback, link-local, multicast, reserved, unspecified, IPv6
site-local, AND unwraps IPv4-mapped-IPv6 first (lines 49-50) so `::ffff:10.0.0.1` is
caught as the RFC1918 address it really is. `METADATA_HOSTS` (line 39) additionally
denies the cloud metadata names/IPs (`169.254.169.254`, `fd00:ec2::254`,
`metadata.google.internal`) by name, before resolution.

### Thread-local DNS pinning closes the resolve/connect TOCTOU

The interesting decision. A plain "resolve, validate, then connect by name" is a TOCTOU
hole: a hostile DNS can rebind the name to an internal IP between the check and
urllib3's own re-resolution at connect time. `_netsafe` closes this by pinning DNS for
the calling thread:

- `_pin` is a `threading.local` holding a `{host: forced_ip}` map (line 63).
- `socket.getaddrinfo` is wrapped once, idempotently, at import (lines 64-79). The
  wrapper `_pinning_getaddrinfo` (line 67) returns the forced IP only when the CURRENT
  thread has a pin for that host; otherwise it calls the original resolver. It is a
  no-op for every other thread and code path - aiohttp uses the loop resolver, not this.
- `safe_open` (line 133): for each hop it resolves+validates via `resolve_safe_ip`, sets
  `_pin.map = {host: pinned}`, makes the request, then clears the pin in a `finally`
  (lines 166-171). urllib3 thus resolves `host` to exactly the validated IP and cannot
  rebind. Every redirect hop is re-resolved, re-validated, and re-pinned (the loop at
  144-181, `allow_redirects=False`, manual `Location` follow up to `max_redirects`).

### Why pin DNS instead of an IP-literal adapter

Documented in the module docstring (lines 15-21): under `requests 2.34` / `urllib3 2.7`
the `HTTPAdapter` `server_hostname` override does not propagate, so connecting to an IP
literal fails TLS SNI (handshake failure). Pinning `getaddrinfo` keeps the real hostname
intact - so SNI, certificate verification, and the `Host` header all work normally -
while still forcing the socket to the validated IP. Do not "simplify" this to an
IP-literal adapter; it will break TLS.

### resolve_safe_ip (line 82)

Resolves a host once and returns ONE IP literal that passes `ip_is_blocked` - the same
IP the connection is then pinned to. Returns `None` if the input is an IP literal that is
blocked, a metadata host, unresolvable, OR if ANY answer in the address set is unsafe
(the all-answers rebinding check, lines 100-113: a single bad answer rejects the whole
host). This is the function `ipintel.py` calls directly to vet a target before querying
reputation sources.

`modules/base.py:resolve_public` (line 73) is the sibling used by the network probers
(`.headers`/`.ssl`/`.tcp`/`.down`): it returns the full `getaddrinfo` list and raises
`ValueError` if any address is non-public. Its docstring (lines 83-87) is honest that it
is resolve-time validation only; callers that connect must use an address from the
returned list rather than re-resolving (the probers connect by the validated IP).

`url_is_safe` (line 116) is the scheme+host pre-flight for handing a user URL to a third
party (e.g. the is.gd shortener): http/https only, host not a metadata host,
`resolve_safe_ip` non-None.

---

## 4. Outbound HTTP size caps (`modules/base.py:fetch_json`, line 27)

Every outbound JSON call should go through `fetch_json`, not `requests.get(...).json()`.
It streams the body and caps it BEFORE decode/parse:

```
with requests.get(url, ..., stream=True) as r:
    if allow_404 and r.status_code == 404: return None
    r.raise_for_status()
    body = r.raw.read(max_bytes + 1, decode_content=True)
    if len(body) > max_bytes: raise ResponseTooLarge(...)
    return json.loads(body.decode("utf-8", errors="replace"))
```

- Default cap `_DEFAULT_MAX_JSON_BYTES = 256 KB` (line 15). Modules with legitimately
  larger payloads pass an explicit `max_bytes=` (poke ~1 MB, numberfact ~4 MB).
- Reading `max_bytes + 1` then comparing `> max_bytes` detects the overflow without
  buffering an unbounded body - a compromised/misconfigured upstream cannot OOM the
  process with a JSON bomb.
- The `with` block guarantees the socket/FD is released on every exit path including the
  404 short-circuit and the `ResponseTooLarge` raise (comment lines 58-60); a leaked
  `stream=True` response leaks the connection.
- `allow_404=True` returns `None` on 404 for lookup-or-miss semantics (dictionary word,
  pokemon name, GreyNoise unseen).

`ipintel.py` uses both the helper and one inline streamed+capped read for the Tor exit
list (`_tor_fetch`, lines 208-221, `_TOR_MAX = 4 MB`) because that endpoint returns text,
not JSON; it applies the same `read(MAX+1)` / `> MAX` pattern by hand.

---

## 5. State-file integrity (`store.py`)

The three state files (locations.json, channels.json, users.json) carry PII (nicks,
hostmasks, timestamps, user-supplied ZIPs) and load into memory at startup. The integrity
model is a checksummed envelope plus quarantine-on-bad-read plus a one-deep backup on
write.

### v2 checksum envelope (lines 42-103)

Current on-disk shape: `{"schema": 2, "checksum": "<sha256>", "data": <payload>}`.
`_checksum` (line 55) is SHA-256 over canonical JSON (`sort_keys=True`,
`separators=(",",":")`) so the same data always hashes the same regardless of dict order
or Python version. `_wrap_v2` (line 67) builds the envelope on write; `_unwrap`
(line 83) validates on read.

Legacy v1 (the bare payload, no `schema` key) is accepted unchanged and silently
re-written as v2 on the next flush (lines 51, 102-103). A v2 envelope with a wrong
schema version, a missing/non-string checksum, or a checksum mismatch raises
`_StoreRejected` (lines 92-100).

### Quarantine on bad envelope (lines 149-189)

`_read` does NOT reset a suspect file to empty and let the next flush overwrite the only
copy - that would silently lose locations, channel-rejoin state, and privacy opt-out
flags. Instead, on any of {oversize (`_MAX_FILE_SIZE = 10 MB`, line 147), JSON decode
error, unicode error, `_StoreRejected`, OR a type mismatch where the payload's type does
not match the expected default's type (BUG-051, lines 162-166)} it calls `_quarantine`
(line 176), which `os.replace`s the file aside to `<name>.corrupt.<unixts>` and starts
from the empty default. The corrupt file stays on disk for manual recovery.

This is the key gotcha for the next maintainer: a checksum mismatch does NOT crash and
does NOT silently wipe - it quarantines and continues with empty state. If a user reports
"the bot forgot all saved locations after a restart," look for `*.corrupt.*` files and a
`Store: ... unusable` log line.

### Backup + atomic write (`_write`, line 191)

- Writes to a `tempfile.mkstemp` in the same dir, `chmod 0o600` BEFORE the atomic
  `os.replace` (lines 204-211) so the final file is never world-readable even briefly
  (POSIX only; Windows ACLs are the operator's job).
- Before replacing, copies the current good file to `<name>.bak` (lines 214-222),
  best-effort - a backup failure logs but does not block the write (the original stays
  intact until `os.replace`). One-deep backup; a second write overwrites it.
- Wraps in the v2 envelope and pretty-prints (`indent=2`) for human inspection.

### Pruning and privacy

`_prune_users` (line 272) removes entries whose `last_seen` is older than
`user_max_age_days` (default 90), EXCEPT records with `opted_out` true (lines 284-288):
an opt-out is a privacy preference that must outlive the inactivity window, or the bot
silently resumes tracking a user who asked it not to. `user_max_age_days` is floored at 1
(line 125): a 0/negative value would set the cutoff to `now` and wipe every tracked user
(and their opt-out flags) on the first flush. `_before` (line 22) parses timestamps
rather than comparing strings, and treats malformed values as stale.

The flush thread swallows-and-continues on any exception (lines 238-247): a flush failure
must never kill the persistence thread, which would silently stop ALL future saves with
no liveness signal. `user_purge` (line 381) hard-deletes every record of a nick for the
`.forgetme` privacy command.

`RateLimiter` (line 464) lives here too: three windows (per-nick flood, per-nick API,
per-channel cross-user burst), all cooldowns floored at 1s (lines 489-490) so a
misconfigured zero cannot silently disable a gate. The per-channel gate does NOT record
an attempt once it is over budget (lines 554-559) so an attacker cannot hold the window
full forever by spamming after the limit trips.

---

## 6. Audit log (`audit_log.py`)

Append-only, HMAC-SHA256-chained, tamper-evident record of privileged actions. Separate
from the main botlog. Every privileged handler in `admin_cmds.py` calls
`audit_log.default().record(nick, host, action, args)` via the `_audit` helper
(`admin_cmds.py:82-94`), which resolves the actor's hostmask from `_nick_hosts` and
catches all exceptions so an audit failure never breaks the admin command.

### The HMAC chain (lines 99-104, 236-294)

Each record's `this_hash = HMAC-SHA256(key, prev_hash ∥ ts ∥ actor ∥ host ∥ action ∥
args_str)`, where the fields are NUL-separated (`_canonical`, line 81 - a NUL delimiter
means a value containing the literal separator cannot collide with a different field
layout). The record stores `prev_hash` (the previous record's `this_hash`) and its own
`this_hash`, forming a chain. Editing, reordering, or deleting any non-tail record breaks
the `prev_hash` link and the HMAC, which `verify` (line 296) reports as
`(False, first_broken_index)`. Exposed via `.audit verify` (`admin_cmds.py:690-698`).

### Why HMAC, not plain SHA-256

The key lives in a `0o600` sidecar `audit.log.key` (line 123). An attacker who obtains
only a copy of `audit.log` (a backup, an accidental commit) cannot recompute the chain to
forge entries, because the hashing algorithm is in this very file - plain SHA-256 (the
pre-3.0.0 scheme) could be recomputed by anyone (docstring lines 5-12).

### Fail-closed key load (`_load_key`, line 131)

The subtle, load-bearing part:

- If the key file EXISTS but is unreadable (transient FS/perms error), it raises
  `RuntimeError` rather than regenerating (lines 138-145). Regenerating would
  `O_TRUNC`-write a new key and silently void every prior record's HMAC, destroying the
  tamper-evidence. The audit caller catches this; the operator fixes the file.
- If the key file exists but is genuinely malformed/short (< 32 bytes), it is moved aside
  to `<name>.key.bad` (NOT truncated over, lines 152-160) so the old chain stays
  recoverable, then a fresh key is generated.
- A fresh 32-byte key (`secrets.token_bytes`) is written `0o600` from `os.open` creation
  (lines 161-170).

### Backward compatibility

Records written before 3.0.0 have no `v` field and were plain-SHA-256-hashed. `verify`
falls back to `_sha_record` for them (lines 336-341) so a pre-3.0.0 log still verifies;
every new record is `v: 2` HMAC. `record` preserves the original args shape (dict/list/
scalar) when JSON-serializable (lines 263-267) so analysis is not degraded to opaque
strings, but hashing always uses the deterministic `_stable_args_str` form (line 63) so
re-walking reproduces the digest.

### Honest limitations (do not oversell)

Stated in the docstring (lines 22-28):

- Pure TAIL truncation by an attacker with write access to BOTH `audit.log` and
  `audit.key` cannot be detected from the file alone - that needs an external append-only
  sink (remote syslog), out of scope for a single-host bot. Editing/reordering/deleting
  any NON-tail record IS caught.
- One `threading.Lock` for in-process serialization only (line 124); NOT safe for
  concurrent writers across processes (no `fcntl` flock).

Rotation: the log rotates to `audit.log.<timestamp>` past `_MAX_BYTES = 5 MB`
(lines 212-232); each rotated segment keeps its own chain and the new file starts from a
fresh genesis. The append path creates the file with `0o600` and re-chmods after every
write (lines 274-284) - it may contain hostmasks, which are PII.

---

## 7. Metrics endpoint bind guard (`metrics.py`)

The Prometheus exporter is disabled by default and imposes zero network footprint until
someone calls `registry.enable()` then `registry.expose(host, port)` (module docstring
lines 1-9). It IS wired into startup, config-gated: `internets.py:1345-1352` reads
config.ini `[metrics]` and, only when `enable = true` (default false), does
`from metrics import registry as _mreg; _mreg.enable(); _mreg.expose(host, port)` with
`host = [metrics] host` (default `127.0.0.1`) and `port = [metrics] port` (default
`9779`). So with that one section set, this is a live, reachable listener, not dormant
code; a misconfigured `host` can bind it off-host (the all-interfaces guard below is the
backstop). The `# TODO(internets.py)` at `metrics.py:9` asking for this wiring is stale -
the wiring already exists - and should be cleared.

`expose` (line 256) has two gates:

- Refuses to start unless `enable()` was called first (lines 263-266) - fail-closed
  default.
- Rejects any all-interfaces bind (lines 274-284). It parses the host with
  `ipaddress.ip_address`, unwraps IPv4-mapped-IPv6, and rejects when the address
  `is_unspecified` OR the host string is empty. This catches `0.0.0.0`, `::`, `::0`,
  `::ffff:0.0.0.0`, and the whitespace forms (`"0.0.0.0 "`) that the old literal denylist
  missed. Loopback is explicitly allowed (the documented reverse-proxy front-end). The
  `nosec B104` at line 281 suppresses Bandit's grep-match false positive - those literals
  are a guard, never a target.

The handler serves only `GET /metrics` (404 otherwise, lines 295-301) and runs on a
daemon thread. The point: this endpoint exposes operational internals and must remain
loopback-only; off-host exposure requires an explicit reverse proxy, never a direct
`0.0.0.0` bind.

---

## 8. IRC-injection defense: strip_ctrl (`modules/base.py:strip_ctrl`, line 177)

The single sanitizer for any third-party/user-derived text spliced into an IRC line (API
titles, redirect `Location` headers, sensor names, user echoes). `_IRC_CTRL_RE`
(line 174) is `[\x00-\x1f\x7f]` - the FULL C0 range plus DEL, not just CR/LF/NUL. This
strips `\x02` (bold), `\x03` (color), `\x16` (reverse), `\x1b` (ESC/ANSI), `\x07` (BEL)
so upstream text cannot inject bot-attributed formatting, ANSI escapes, or terminal-bell
spoofing. It coerces non-str (int/None) to str first and caps length (default 400).

Division of labor: the IRC sender strips only `\r\n\x00` as a transport backstop, so
`strip_ctrl` is the REAL defense against formatting/escape injection. Modules emitting
upstream-derived text must route it through here.

### The enforcement test

`tests/run_tests.py:234-245` is a COMPLETENESS gate, not a change-detector. It enumerates
the security-relevant modules (`search`, `seen`, `tell`, `stocks`, `remind`, `location`)
and asserts each source references `strip_ctrl`; `weather` may use its own `_sanitize`
(same C0/DEL regex). It catches a future module - or a removed call - that drifts off the
canonical sanitizer. `tests/test_modules_base.py:13-33` separately tests `strip_ctrl`'s
behavior (strips control bytes, preserves UTF-8, coerces None/int, enforces max_len).

Gotcha: a line that intentionally includes `\x02` emphasis (e.g. an `ipintel` verdict)
must NOT be re-run through `strip_ctrl` after assembly - that would delete the emphasis.
The pattern (see `ipintel._format`, below) is: strip_ctrl EACH untrusted field
individually, then strip only transport bytes (`[\r\n\x00]`) from the assembled line.

---

## 9. IP-reputation commands (`modules/ipintel.py`) - QUERY-ONLY

`.ip` / `.rep` is a keyless multi-source IP reputation AGGREGATOR. It is strictly
read-only: it QUERIES reputation about a target IP and prints a one-line summary. It does
NOT report, submit, contribute, or feed any IP to any blocklist, DNSBL, honeypot, or
threat-intel pipeline. There is no outbound write to any of these services - every call
is a GET/lookup. Do not document or describe it as feeding a pipeline.

### Sources (`_aggregate`, line 352)

All run concurrently in worker threads via `asyncio.gather(..., return_exceptions=True)`:

- DNSBL: 6 zones (`_DNSBL_ZONES`, lines 50-57: DroneBL, SpamCop, PSBL, UCEPROTECT, s5h,
  GBUdb) queried over Cloudflare DNS-over-HTTPS (`_DOH_URL`, line 63). IPv4 only (the
  zones are IPv4-only; IPv6 targets report `DNSBL n/a`, `_dnsbl_name` lines 91-103). A
  listed answer is an A record in `127.0.0.0/8` EXCLUDING the `127.255.255.0/24`
  "query refused / public-resolver" sentinel (lines 58-61, 128). Spamhaus ZEN is
  deliberately NOT included (docstring lines 19-23): it refuses public resolvers and
  would always read as clean, which is worse than absent.
- SANS ISC / DShield (`_dshield_sync`, line 140).
- GreyNoise community (`_greynoise_sync`, line 160); 404 -> `{"classification": "unseen"}`.
- Tor bulk exit list (`_tor_is_exit`, line 224), cached `_TOR_TTL = 3600`s behind
  `_tor_lock`.
- AbuseIPDB (`_abuseipdb_sync`, line 180) - the ONLY keyed source, optional
  `abuseipdb_key`; the command degrades gracefully without it (lines 182-183, 342-344).

Each helper catches all errors and returns a per-source sentinel (`-1`/`None`) so one
dead source never breaks the whole reply (the `_NET_ERRORS` tuple + bare-except backstops
throughout).

### Safety model (docstring lines 11-23)

- The target is validated by `_TARGET_RE` (line 45, conservative charset) then resolved
  to ONE public IP through `_netsafe.resolve_safe_ip` (line 416, run in a thread because
  DNS is blocking). Private/loopback/link-local/reserved/unresolvable targets are refused
  BEFORE any request goes out (lines 417-422), so an internal IP can never be leaked to a
  third party.
- The validated IP only ever appears as a query param / path segment against FIXED,
  trusted endpoints - never a user-controlled URL - so there is no SSRF surface here the
  way there is for `probe`/`scinews`.
- Every upstream string is `strip_ctrl`'d per-field; the assembled line is then stripped
  for transport bytes only (`_TRANSPORT_RE`, line 82) so intentional `\x02` emphasis
  survives (`_format`, lines 266-327). Output is capped at `_MAX_LINE = 400`.
- Rate-limited per-nick via `bot.rate_limited` (the `_gate` check, lines 346-350).

The `_verdict` function (line 253) is pure and unit-testable: clean / suspicious /
malicious from DNSBL listing count, Tor membership, GreyNoise classification, AbuseIPDB
score, and DShield report count.

---

## Cross-cutting invariants for the next maintainer

- Authorization is re-derived per call from the live hostmask; never cache an "is this
  nick an admin" boolean across events.
- Secrets are reversible: never hash a secret, never log its value, never print it from a
  CLI.
- Any new module fetching a user-influenceable URL uses `_netsafe.safe_open` /
  `resolve_safe_ip` (or `resolve_public` for raw-socket probers); any new outbound JSON
  uses `fetch_json` with an appropriate `max_bytes`.
- Any new module splicing upstream/user text into an IRC line routes it through
  `strip_ctrl`, and gets added to the completeness gate in `tests/run_tests.py` if it is
  security-relevant.
- Guards fail closed: bad perms -> refuse to read secrets; bad checksum -> quarantine,
  not wipe; unreadable audit key -> refuse, not regenerate; registry not enabled or
  all-interfaces bind -> refuse to expose.
