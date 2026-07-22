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

The authorization decision is `IRCBot.is_admin(nick)` (`internets.py:357`). It is the
single privileged-boundary check; `admin_cmds.py:_require_admin` (line 76) just wraps it
with a user-facing "auth first" message. Every privileged `cmd_*` calls `_require_admin`
first.

### Auth state

Two dicts, both guarded by one lock `self._auth_lock` (`internets.py:238`):

- `self._authed: dict[str, str]` (line 227) - lowercased nick -> the hostmask bound at
  auth time.
- `self._nick_hosts: dict[str, str]` (line 271) - lowercased nick -> the most recently
  observed hostmask for that nick.

The lock guards BOTH dicts together (the comment at line 225 says so) because `is_admin`
reads both and must see a consistent pair. `_nick_hosts` is written ONLY on inbound
PRIVMSG (`internets.py:1108`) and NICK (`1054-1055`); popped on QUIT (`1043`) and cleared
on reconnect (`1254`). JOIN/CHGHOST/ACCOUNT mutate the persistent Store, not `_nick_hosts`.

### is_admin re-checks the live binding, fail-closed

`is_admin` does NOT just test membership in `_authed`. It re-derives authorization from
the CURRENT hostmask on every call (`internets.py:357-377`):

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
`_AUTH_CLEANUP_THRESHOLD = 50` (`internets.py:170-172`). Failures tracked in
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
  (`internets.py:1128`).
- `verify_password` exceptions are handled in two tiers (`admin_cmds.py:149-170`): a
  `ValueError` (known hashpw config error, no password content) is logged with its
  message; ANY other backend exception is logged as `type(e).__name__` only, because
  argon2/bcrypt/scrypt backends occasionally echo input or hash fragments in exception
  text. The unexpected-exception path also counts as a failed attempt.
- The audit record for a successful auth passes `None` as args (`admin_cmds.py:189`),
  never the password or a derivative.

### A session is a routing handle, not an authz boundary

The session keys on nick, but nick is a routing handle that the network can reassign.
Authorization is therefore re-bound to the hostmask on every `is_admin` call; the session
(`_authed`) is popped only by QUIT (`1044`) and NICK (`1060`). Handled in
`_handle_membership` (`internets.py:1016`):

- QUIT (`internets.py:1065-1078`): drop the cached hostmask AND pop any `_authed` entry -
  a reconnector reusing the nick must re-auth.
- NICK (`internets.py:1079-1098`): the session is DROPPED, not migrated to the new nick.
  The comment at 1056-1059 records why: migrating let a malicious server or a
  nick-takeover launder an authed session onto an attacker-chosen nick.
- CHGHOST / ACCOUNT (`internets.py:1017-1036`): update the persistent Store via
  `user_rename`; they do NOT write `_nick_hosts`. A host change reaches `is_admin` only
  after the user's next PRIVMSG (`1076`) or NICK (`1055`); CHGHOST/ACCOUNT alone do not
  revoke.
- Global drops: `_authed.clear()` on reconnect/disconnect paths
  (`internets.py:1281-1285`, `1327-1329`; `admin_cmds.py:470-472`).

Concurrency note: the `_auth_lock` guards `_authed`/`_nick_hosts` against a torn read of
the pair. Today both `is_admin` and the membership mutators run on the event-loop thread
(`flood_limited` -> `flood_check` -> `is_admin`, `internets.py:387`), so the lock is
defensive - load-bearing only if a future free-threaded / GIL-free build moves `is_admin`
onto a worker thread.

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
fallback for 2.4.0-and-earlier upgrades.

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
equality branches in `_cmd_list` (lines 535-543) exist to break CodeQL's
`py/clear-text-logging-sensitive-data` taint propagation; that is deliberate, do not
"simplify" them back into a tainted flow. Separately, `migrate`'s 0o600-tightening log
line deliberately OMITS the config path (lines 479-482) for the same heuristic, which
taints any variable flowing through that function - that omission is intentional too.

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
lines 1-9). It IS wired into startup, config-gated: `internets.py:1377-1384` reads
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

## 10. Admin password hashing (`hashpw.py`) - LOCAL, verify-only secret

`hashpw.py` hashes the bot's local admin password (`config.ini [admin] password_hash`),
checked by `cmd_auth` (Section 1). This is a fundamentally different secret class from
everything in Section 2: verification only needs to confirm the hash matches, never
recover the original password, so it can and must be a one-way hash. Do not follow this
module's pattern for any credential the bot sends on the wire (NickServ password, an API
key) - those are reversible-by-necessity and belong in `secret_store.py`, in
plaintext-at-rest behind `0o600` perms, never hashed (`secret_store.py:3-6` states the
reversibility requirement, `secret_store.py:10` and `:17` the `0o600` file-backend
storage; cross-referenced in Section 2). Hashing a transmitted credential does not
"harden" it; it breaks the auth that needs the original value back.

One qualifier: the admin password is not wire-free either. `cmd_auth`'s own usage string
is `/MSG <bot> AUTH <password>` (`admin_cmds.py:118`) - the plaintext password crosses the
IRC link in the clear on every login, same as any other command argument. "LOCAL,
verify-only" describes what the bot does with the value after receipt (hash comparison,
never stored or replayed anywhere else); it does not mean the value never transits a wire.

### Supported formats and dispatch (`verify_password`, line 211)

Three formats, preference order strongest-first per the module docstring (lines 9-15):

| prefix    | algorithm | dependency                        | stored format                                   |
|-----------|-----------|------------------------------------|--------------------------------------------------|
| `argon2$` | argon2id  | `argon2-cffi` (optional extra)     | `argon2$<argon2-cffi encoded hash>` (line 206)    |
| `scrypt$` | scrypt    | stdlib `hashlib.scrypt`, no extra  | `scrypt$N$r$p$<salt b64>$<dk b64>` (line 157)     |
| `bcrypt$` | bcrypt    | `bcrypt` (optional extra)          | `bcrypt$<bcrypt hash>` (line 185)                 |

The `argon2$` stored format has a literal double dollar sign, not a single one:
`hash_argon2` concatenates the prefix `"argon2$"` with `ph.hash(password)` (`hashpw.py:206`),
and argon2-cffi's own PHC-format output already begins with `$`
(`$argon2id$v=19$m=...,t=...,p=...$<salt>$<hash>`), producing `argon2$$argon2id$v=19$...`.
`_verify_argon2` relies on this: `stored.split("$", 1)[1]` (`hashpw.py:264`) splits on only
the first `$`, so the recovered payload keeps its own leading `$` and remains a valid PHC
string for `PasswordHasher().verify()`. A future rewrite that strips the `argon2$` prefix
and also swallows the PHC string's leading `$` would silently break every stored argon2
hash.

Dispatch is a plain `str.startswith` chain on the stored value (lines 221-226) - the
stored hash carries both its algorithm tag and its own cost parameters, so raising the
defaults in `_argon2_params`/`_bcrypt_rounds`/`_best_scrypt_params` never invalidates
existing hashes; only hashes created after the bump get the new cost
(`verify_password` docstring, lines 214-217). That docstring's last sentence points to
"See KEY_ROTATION.md" (`hashpw.py:217`) - no such file exists anywhere in the repo; treat
it as stale documentation debt, not a pointer to follow.

argon2id is RECOMMENDED for new deployments (OWASP 2024 first choice, memory-hard and
side-channel resistant per lines 11-12), but **scrypt is the CLI default**
(`main`, line 298) purely for backwards compatibility - flipping the default wouldn't
break old hashes (format is self-describing) but would surprise operators who expect a
stable default (lines 17-20). `main` prints a recommendation to use `--algo argon2`
whenever a different algo is picked (lines 302-304).

### scrypt cost is host-dependent: `_best_scrypt_params` degrades silently

`hash_scrypt` does not hash at a fixed cost. `_best_scrypt_params` (`hashpw.py:122-145`)
probes 8 descending `(N, r, p)` sets, from `N=2**17` (OWASP 2024 recommended) down to
`N=4096` (`hashpw.py:130-139`), and uses the first one the host's OpenSSL build accepts
for a throwaway probe hash - OpenSSL enforces a per-process memory cap that the stdlib
wrapper inherits (comment at `hashpw.py:115-120`). A host with a tighter memory cap
therefore gets a weaker hash than a host with headroom; `main` does print whichever
`(N, r, p)` was actually used, after the fact (lines 321-323), but nothing compares that
choice to the OWASP-recommended `N=2**17` or warns the operator that degradation occurred.
If every set in the list fails,
`_best_scrypt_params` raises `RuntimeError` (`hashpw.py:145`) - a third failure mode
alongside the two `verify_password` outcomes documented below, and one that surfaces only
during hashing, never during verification.

### verify_password: raises ValueError vs returns False - do not conflate them

Each outcome has its own dedicated test:

- **Unrecognised prefix -> raises `ValueError`.** An empty/`None` stored value
  (`if not stored`, line 219) or a prefix that isn't one of the three above (line 227-229)
  raises. `tests/test_hashpw.py:194-205` (`TestVerifyDispatch`) pins this for empty
  string, `None`, and `"md5$deadbeef"`. `admin_cmds.py:cmd_auth` treats this ValueError
  as a config error, not a login failure - it does NOT count against the brute-force
  lockout (`admin_cmds.py:149-156`), because an unrecognised format means the operator
  misconfigured `config.ini`, not that an attacker guessed wrong. In practice only the
  unknown-prefix half of this branch is reachable from `cmd_auth`: it already returns
  early on `if not h` (`admin_cmds.py:113-116`) before ever calling `verify_password`, so
  the empty/`None`-stored raise (`hashpw.py:219-220`) fires only when `verify_password` is
  called directly with an empty or `None` argument - as
  `tests/test_hashpw.py:195-201` does (`TestVerifyDispatch.test_empty_stored_raises` /
  `test_none_stored_raises`) - never through the live `.auth` path. The module's own
  self-test always passes a freshly produced `hashed` value (`hashpw.py:319`, `345-348`),
  which is never empty, so it cannot trigger this branch either.
- **Recognised prefix, malformed/wrong payload -> returns `False`.** Once the prefix
  dispatches into `_verify_scrypt`/`_verify_bcrypt`/`_verify_argon2`, parsing and
  verification failures - bad base64, wrong field count, a garbage bcrypt/argon2 payload,
  or simply a wrong password - are caught and converted to `False` (lines 242-243,
  253-254, 265-266). This does NOT cover a missing optional dependency:
  `_verify_bcrypt`/`_verify_argon2` catch `ImportError` in a separate, earlier `try` block
  and re-raise it as `ValueError` instead (lines 249-250, 261-262) - see "bcrypt and
  argon2-cffi are optional" below. `test_verify_garbage_returns_false`
  (`tests/test_hashpw.py:166-168`) and `test_verify_invalid_hash_returns_false`
  (`tests/test_hashpw.py:188-189`) pin the False path directly against
  `_verify_bcrypt`/`_verify_argon2` with hand-crafted garbage payloads;
  `test_cross_algo_wrong_password_is_false_not_error`
  (`tests/test_hashpw.py:207-211`) pins that a wrong password is `False`, never an
  exception. This path DOES count as a failed login attempt in `cmd_auth`
  (`admin_cmds.py:190-197`).

`cmd_auth` also has a third outcome the split above does not cover: any exception other
than `ValueError` escaping `verify_password` - a bug in `hashpw.py` or one of its backends,
not a documented condition - is caught by a catch-all `except Exception`
(`admin_cmds.py:157-170`). It logs only `type(e).__name__`, never the exception text, and
counts the attempt against the brute-force lockout, the same as a wrong password
(`admin_cmds.py:163-168`). This is deliberate defence in depth, not a config-error path.

Why the split matters operationally: `botlog.py:_validate_hash` (line 180) calls
`sys.exit(1)` at startup if the configured `password_hash` prefix is invalid (lines
200-206) specifically because it knows `verify_password` would otherwise raise on every
auth attempt, silently disabling admin commands with no obvious symptom to a user typing
`.auth`. Better to fail loud at process start than fail closed-but-silent on first login.
An empty hash is NOT fatal at startup (lines 188-189, 192-194) - the bot runs with auth
disabled, which is the expected first-run state before an operator has run `hashpw.py`.

This startup guard is not continuously enforced, though: `_validate_hash` runs exactly
once, at import (`botlog.py:210`), while `get_hash()` calls `reload_config()` and re-reads
`password_hash` from disk on every `.auth` attempt (`botlog.py:164-174`, called from
`admin_cmds.py:113`). Editing `config.ini` to an unrecognised prefix after startup bypasses
the `sys.exit(1)` guard entirely - the bot keeps running, and every subsequent `.auth`
falls into the unrecognised-prefix `ValueError` branch above instead of failing at startup.

`botlog.py:_VALID_HASH_PREFIXES = ("scrypt", "bcrypt", "argon2")` (`botlog.py:177`) is a
second, hand-maintained enumeration of the same three algorithms `verify_password`
dispatches on (lines 221-226). Adding a fourth algorithm to `hashpw.py` without updating
this tuple makes the bot refuse to start on a hash `hashpw.py` can verify perfectly fine.

### bcrypt and argon2-cffi are optional; scrypt is not

Both extras are declared in `pyproject.toml:45-46` (`bcrypt`, `argon2`) and imported
lazily, inside the function body, not at module load (`hash_bcrypt` line 181,
`hash_argon2` line 198, `_verify_bcrypt` line 248, `_verify_argon2` line 259) - so
`import hashpw` never fails regardless of what's installed, and the scrypt path stays
fully functional with zero extra packages. On `ImportError`:

- The **hashing** functions call `sys.exit(...)` (lines 182-183, 199-200). This is not
  CLI-only: `hash_bcrypt`/`hash_argon2` are also called directly by the test suite
  (`tests/test_hashpw.py:155, 159, 163, 175, 181, 185`), so those tests rely on `bcrypt`
  and `argon2-cffi` being installed. A missing extra surfaces there as a single
  `SystemExit` test failure, not a process abort - pytest catches `SystemExit` in the
  call phase and the rest of the suite still runs.
- The **verify** functions instead raise `ValueError` (lines 249-250, 261-262) - `_verify_*`
  runs inside `cmd_auth`'s live auth path (`admin_cmds.py:148`, on a worker thread via
  `asyncio.to_thread`), where `sys.exit` would kill the whole bot process over one admin's
  auth attempt. The caller's `except ValueError` (`admin_cmds.py:149-156`) turns this into
  a "config error, see log" reply instead. Net effect: an admin whose stored hash is
  `bcrypt$...` but who removed the `bcrypt` package from the venv gets a clean error
  message on `.auth`, not a bot crash - but they also cannot authenticate until the
  package is reinstalled.

### scrypt derived-key length follows the stored hash, not the hasher

`hash_scrypt` uses a 32-byte salt and a 64-byte derived key
(`salt = os.urandom(32)`, `dklen=64`, `hashpw.py:155-156`) - both wider than argon2id's
`_ARGON2_SALT_LEN = 16` / `_ARGON2_HASH_LEN = 32` (`hashpw.py:70-71`). `_verify_scrypt`
does not pin the recomputed key to that 64-byte length: it derives with
`dklen=len(expected)` (`hashpw.py:239`), i.e. the output length is taken from whatever was
decoded out of the stored hash, not from a constant matching `hash_scrypt`. A stored
derived key that was truncated (corrupted config, a bug in a future `hash_scrypt`
variant) would therefore shorten the comparison instead of failing outright - `_ct_eq`
(below) compares two equal-length values and could still return `True` for a key that
never should have verified. This is worth flagging if `_verify_scrypt` is ever touched -
`dklen` is derived from the stored value's own length rather than pinned to a constant -
but it is not a currently-exploitable bug: today `dklen` and the stored key's length are
always in lockstep because both trace back to the same `hash_scrypt` call.

The same applies to `N`, `r`, and `p` themselves: `_verify_scrypt` derives all three from
the stored hash, not from `hash_scrypt`'s own defaults (`hashpw.py:239`). `config.ini` is
0600-trusted, so this is not attacker-reachable in the current threat model, but an
inflated `N` in a corrupted or hand-edited config would make the auth worker thread
attempt that allocation before `MemoryError` is caught and converted to `False`
(`hashpw.py:242-243`).

### Timing: constant-time comparison only where the library doesn't already provide it

`_ct_eq` (line 269-270) wraps `hmac.compare_digest`, applied once, in `_verify_scrypt`
(line 241) to compare the recomputed derived key against the stored one - `hashlib.scrypt`
returns raw bytes with no built-in comparator, so a naive `==` would data-dependently
short-circuit on the first mismatched byte and leak timing information. `_verify_bcrypt`
and `_verify_argon2` do NOT call `_ct_eq` themselves because `bcrypt.checkpw` (line 252)
and `PasswordHasher.verify` (line 264) already perform constant-time comparison
internally - re-wrapping them would be redundant, not more correct.

What is NOT constant-time, and why it's an accepted gap: `verify_password`'s prefix
dispatch (lines 221-226) and the cost parameters embedded in a scrypt/argon2/bcrypt hash
mean verification time varies by algorithm and by configured cost - an observer who can
already read `config.ini` learns nothing new from this (they'd see the prefix and cost
directly), and a remote attacker only ever gets the aggregate latency of the full
`cmd_auth` await, not a per-byte comparison signal.

### CLI entry point

```
python hashpw.py                  # scrypt (default)
python hashpw.py --algo bcrypt
python hashpw.py --algo argon2
```

`main` (line 295) prompts twice via `getpass.getpass` (never echoed, never accepted as a
CLI argument). `hashpw.py` itself states no rationale for this choice. Contrast
`secret_store.py`'s own `set` command, which accepts a secret value via the optional
`--value` flag (the only positional argument on `set` is the secret's name,
`secret_store.py:684`) and prompts only when `--value` is omitted - its stated reason is
"safer for shell history" (`secret_store.py:686`), not process-listing exposure;
`secret_store.py` has no argv/process-listing rationale anywhere in the file. `main`
rejects a mismatch or a length outside `[8, 1024]` characters
(lines 310-315), hashes, and prints the exact `config.ini` line to paste under `[admin]`
(lines 340-343).

The `[8, 1024]` range is `hashpw.py`'s own policy at hash-creation time; it is not the
effective password policy end to end. `admin_cmds.py:120-122` rejects any `AUTH` argument
longer than 128 characters before it ever reaches `verify_password`, so a password of
129-1024 characters can be hashed successfully by `hashpw.py` and then never authenticate
over IRC - the two limits disagree by 8x, and `hashpw.py`'s self-test (below) cannot catch
this because it never goes through `cmd_auth`.

A second, similar mismatch: `cmd_auth` verifies `arg.strip()` (`admin_cmds.py:148`), but
`main` hashes `pw` exactly as `getpass.getpass` returned it, with no stripping before the
length checks or the hash call (`hashpw.py:308-319`). A password with leading or trailing
whitespace hashes and self-tests successfully in `hashpw.py`, then can never authenticate
over IRC, because the stripped value `cmd_auth` checks never matches the hash of the
unstripped one.

It then self-tests by calling `verify_password` against both the correct
and an incorrect password before declaring success (lines 345-349) - a hashing bug that
produced a hash nothing could ever verify against would otherwise ship silently. It also
flags parameter weakness at the extremes: under `_FAST_HASH_THRESHOLD_S = 0.050` warns the
cost is too low for a 2026 GPU/ASIC attacker (lines 333-335), over
`_SLOW_HASH_THRESHOLD_S = 1.000` notes it may be too slow for login UX and points at the
tuning env vars (lines 336-338).

The comment above these thresholds (`hashpw.py:288-289`) claims the module "back[s] off
automatically (drop memory by 25%, then time_cost by 1)" past the slow threshold - no such
back-off exists anywhere in the module; the only thing that happens past
`_SLOW_HASH_THRESHOLD_S` is the printed NOTE quoted above. Treat that comment as the same
class of stale documentation as the `KEY_ROTATION.md` reference noted earlier.

Cost is tunable without touching source via `INTERNETS_ARGON2_MEM_MIB` (default 128,
clamped to `[19, 4096]` MiB), `INTERNETS_ARGON2_TIME` (default 3, clamped to `[1, 20]`),
and `INTERNETS_BCRYPT_ROUNDS` (default 13, clamped to `[10, 16]`) - ranges set by
`_ARGON2_MEM_MIN_MIB`/`_ARGON2_MEM_MAX_MIB` (`hashpw.py:73-74`),
`_ARGON2_TIME_MIN`/`_ARGON2_TIME_MAX` (`hashpw.py:76-77`), and
`_BCRYPT_MIN_ROUNDS`/`_BCRYPT_MAX_ROUNDS` (`hashpw.py:163-164`). `_env_int`
(`hashpw.py:80-95`) enforces each range and has three distinct paths, not two: an unset,
empty, or whitespace-only value returns `default` silently, with nothing logged
(lines 82-84); a non-integer value falls back to `default` with a logged warning
(lines 87-90); and an out-of-range value is clamped to the nearest bound, also with a
logged warning (lines 91-94) - so a misconfigured env var can't silently produce a
near-zero-cost hash (or, at the other extreme, a multi-GiB argon2 allocation that OOMs
the process), though the unset/empty case falls back to `default` with no warning at all.

Argon2 parallelism is not one of the tunable knobs: `_ARGON2_PARALLELISM = 4`
(`hashpw.py:69`) is a fixed constant with no corresponding env var, unlike memory cost and
time cost - only two of the three OWASP-recommended argon2id parameters are
operator-tunable here.

Raising `INTERNETS_ARGON2_MEM_MIB` or `INTERNETS_ARGON2_TIME` only affects hashes created
after the change. `_verify_argon2` constructs `PasswordHasher()` with library defaults
(`hashpw.py:264`) and never calls `check_needs_rehash`, so an already-stored hash is never
flagged for re-hashing at the new cost and no upgrade path is surfaced to the operator; the
only way to move an existing hash to the new parameters is to run `hashpw.py` again and
replace `password_hash` in `config.ini`.

## 11. Admin command surface (`admin_cmds.py`)

`AdminCommandsMixin` (`admin_cmds.py:49`) supplies every `cmd_*` handler mixed into
`IRCBot`. It owns no state of its own - `_authed` (`internets.py:240`), `_auth_fails`
(`internets.py:241`), the shadow-ban set `_shadow_bans`/`_shadow_ban_reasons`
(`internets.py:267-268`), the module registry `_modules` (`internets.py:235`), and
`_nick_hosts` (`internets.py:281`, declared separately later in `__init__`, not part of
that same block) all live on `IRCBot`. The mixin declares its own type-checker stub
block for most of that state and for the `IRCBot` methods it calls
(`admin_cmds.py:52-72`), but the stub block is incomplete: it covers `_nick`, `_authed`,
`_auth_fails`, `_auth_lock`, `_mod_lock`, `_nick_hosts`, `_modules`, `_commands`, the
three `_AUTH_*` constants, and 7 method stubs (`preply`, `send`, `is_admin`,
`load_module`, `unload_module`, `reload_module`, `request_shutdown`). It omits
`_shadow_bans`/`_shadow_ban_reasons` entirely. Only `_shadow_bans` is guarded with
`hasattr`, in `cmd_shadow_ban` and `cmd_shadow_unban` (`admin_cmds.py:823`, `846`);
`_shadow_ban_reasons` is written and popped via a plain attribute access with no guard
of its own (`admin_cmds.py:831`, `850`), and `_shadow_bans` itself is mutated directly
at `admin_cmds.py:829`, `849` - it also omits `privmsg`, `cfg`,
`_store`, `active_channels`, and `_save_shadow_bans`, all of which handlers call
directly with no stub backing them. This section inventories the commands and their
blast radius; for the `is_admin` authorization mechanism itself (fail-closed hostmask
re-binding, session revocation on QUIT/NICK) see section 1 above - it is not repeated
here.

### Command inventory

Most privileged handlers open with `if not self._require_admin(nick, reply_to): return`
(`admin_cmds.py:76-80`), which is a thin wrapper over `is_admin`. `.auth`, `.help`,
`.version`, and `.modules` are reachable without an admin session, and so is `.deauth`:
`cmd_deauth` (`admin_cmds.py:202-213`) contains no `_require_admin` call at all. It is
self-limiting rather than gated - an unauthenticated nick can invoke it and simply gets
"not authenticated" back, since there is no session in `_authed` to delete
(`admin_cmds.py:205-208`). `tests/test_admin_cmds.py:430-454` parametrizes the
non-admin-refused check over 22 handlers and deliberately excludes `cmd_deauth`.

Dispatch still keeps `.deauth` PM-only, same as `.auth`: `_dispatch` refuses either
command with a "must be used in PM" notice before a task is ever created for the handler
(`internets.py:620-621`), so a channel invocation of `.deauth` never reaches
`cmd_deauth` at all, even though the handler has no PM check of its own.

| Command | Gate | Audit-logged | Blast radius |
| --- | --- | --- | --- |
| `.auth <pw>` | none (grants admin) | yes on success only, `args=None` - failures and lockouts are never audited, only `log.warning`'d; see below. | Brute-forceable only up to the lockout; see below. |
| `.deauth` | none - self-limiting, not `_require_admin`-gated | yes, only if a session existed | Ends own session; no-op with "not authenticated" otherwise. Low. |
| `.help` / `.help <x>` / `.help all` / `.help admin` | public | no | Read-only. `.help admin` and hidden-module names are gated on `is_admin` for visibility only (`admin_cmds.py:260-263`, `290`). |
| `.version` | public | no | Read-only. |
| `.modules` | public | no | Read-only, unauthenticated - anyone can enumerate every loaded module and, via a `MODULES_DIR` glob filtered only for `__init__`/`base`/`geocode`/`units` (`admin_cmds.py:370-374`), every on-disk-but-unloaded module too. Minor information-disclosure surface. |
| `.load <mod>` | admin | yes (unconditionally - see below) | **High.** `exec_module`s `modules/<name>.py` (`internets.py:476-478`) - arbitrary Python runs with the bot's full process privileges. Name is regex-constrained (`^[a-z][a-z0-9_]*$`) and path-traversal-checked (`internets.py:464-474`), but anything already sitting in `MODULES_DIR` is trusted to run unsandboxed. |
| `.unload <mod>` | admin | yes (unconditionally) | Medium. Drops a module and its commands; reversible via `.load`. |
| `.reload <mod>` | admin | yes (unconditionally) | **High**, same as `.load` - it unloads then re-`exec_module`s the file from disk, so an admin (or anyone who can write into `MODULES_DIR` between load and reload) gets a second arbitrary-code-execution point. |
| `.reloadall` | admin | yes | Same as `.reload`, fanned out over every loaded module. |
| `.restart` | admin | yes | High. Full process restart via `request_shutdown` + `_restart_flag` (`admin_cmds.py:431-432`). Denial of service if abused. |
| `.rehash` | admin | yes | Medium. Re-reads `config.ini` + `config.local.ini`; on success, `lvl = getattr(logging, new_level, None)` (`admin_cmds.py:449`) is checked with no validation against botlog's `VALID_LEVELS` - only when `lvl` is truthy (`NOTSET` resolves to `0` and is silently skipped) does it reset the log-filter base level, set `log_filter.global_debug = False`, and call `clear_subsystems()` (`admin_cmds.py:451-453`), wiping every per-subsystem debug override. Clears every admin session (`admin_cmds.py:470-472`) only if it reaches that line - see below for the two error paths that return first and leave sessions intact. |
| `.mode <+/-modes>` | admin | yes | Medium. Sends `MODE <bot-nick> <modes>` after a charset check (`^[a-zA-Z+\- ]+$`, `admin_cmds.py:485`); no semantic validation of the mode letters, so a bogus string just bounces off the server. |
| `.snomask <+/-flags>` | admin | yes | Medium, hardcoded to `+s`; charset check is stricter than `.mode`'s - no spaces allowed (`^[a-zA-Z+\-]+$`, `admin_cmds.py:497`, vs. `.mode`'s `^[a-zA-Z+\- ]+$`, `485`), so a multi-flag snomask with a space is refused. |
| `.raw <IRC line>` | admin | yes | **High - flagged.** Injects a raw, otherwise-unvalidated IRC protocol line straight onto the wire (`admin_cmds.py:504-521`). Only CR/LF/NUL and the 510-byte line cap are enforced (`512-517`); the *command* itself (WHOIS, KILL, OPER, SAMODE, ...) is whatever the admin types and whatever the ircd will accept from this connection. |
| `.say [target] <text>` | admin | yes | Medium/high - impersonation. Speaks as the bot to any target; see "Reply path" below for what is and is not sanitized. |
| `.act [target] <text>` | admin | yes | Same as `.say`, wrapped as CTCP ACTION. |
| `.nick <newnick>` | admin | yes | Medium. Requests a nick change; the local `_nick` is updated only on server confirmation, in the `_RE_NICK` handler when the server's own NICK echo names the bot's current nick (`internets.py:1079-1082`), not pre-emptively. |
| `.uptime` | admin | no | Read-only. |
| `.stats` | admin | no | Read-only; exposes queue depth, memory RSS, audit record count. |
| `.audit [N \| grep <pat> \| tail \| verify]` | admin | no | Read-only viewer over the audit log, including the HMAC-chain `verify` check (`admin_cmds.py:690-698`). See ".audit: argument grammar and failure modes" below for the full grammar. |
| `.fingerprint <nick>` | admin | no | Read-only but privacy-sensitive - aggregates hostmask, channel presence, shadow-ban status, `.seen`/`.tell`/`.notes` data, and audit-log mentions for one nick into a single reply (`admin_cmds.py:731-803`). See "Audit log split" below for why this one is not itself logged. |
| `.shadow-ban <nick> [reason]` | admin | yes | **High - flagged.** Silently drops all of a nick's commands and excludes them from module `on_raw` fanout, with no signal to the target that anything changed (`admin_cmds.py:805-836`, dispatch-side enforcement `internets.py:617-619`, `846-862`). Refuses to target only the bot itself or the calling admin (`admin_cmds.py:817-822`) - another admin is a valid target, and because the drop happens in `_dispatch` ahead of every admin gate, one admin can silently lock another out of every command including `.deauth` and `.shutdown`; persisted to disk, see below. |
| `.shadow-unban <nick>` | admin | yes | Lifts a shadow-ban; persisted to disk, see below. |
| `.shadow-list` | admin | no | Read-only listing of active bans. |
| `.loglevel [LEVEL \| <logger> LEVEL]` | admin | yes, only when a change was actually applied | Runtime log-level/subsystem change. Low. |
| `.debug [args]` | admin | yes | Toggles debug logging. Low. |
| `.shutdown` / `.die` | admin | yes | **High - flagged.** Terminates the process via `request_shutdown` (`admin_cmds.py:896-905`). Denial of service if abused; no confirmation step. |

`.load`/`.unload`/`.reload` all discard the success flag returned by their underlying
`load_module`/`unload_module`/`reload_module` calls - `_, msg = self.load_module(mod)`
(`admin_cmds.py:387`, `396`, `405`) - and audit unconditionally regardless of outcome.
An audit record for `load`/`unload`/`reload` therefore does not imply the operation
succeeded; the reply text (`msg`) is the only place the actual result is visible.

All of the above also pass through dispatch-level guards that apply uniformly regardless
of admin status: a 400-char argument cap (`internets.py:169,622-623`), a 50-slot
concurrent-task cap (`_MAX_TASKS`, `internets.py:166,627-631`), and a 60s per-command
timeout that cancels a wedged handler rather than letting it starve the task pool
(`_CMD_TIMEOUT`, `internets.py:167,663-670`) - including admin ones, so a hung `.load`
cannot itself become the denial-of-service.

### Authorization path

Every gated handler calls `_require_admin` (`admin_cmds.py:76-80`), which calls
`self.is_admin(nick)` and, on failure, replies with the auth hint - it adds no logic of
its own. `is_admin` is `internets.py:357`, fully covered in section 1: it re-derives
authorization from the *current* hostmask on every call and is fail-closed on an
unverifiable binding. Nothing in `admin_cmds.py` caches or shortcuts that check.

### `cmd_auth`: input guards, rate limiting, and the refuse-unknown-hostmask rule

`cmd_auth` (`admin_cmds.py:98`) is PM-only (enforced at dispatch, `internets.py:620-621`,
not in the handler itself). Ahead of the lockout logic it applies three input guards:

- If `get_hash()` returns empty - no `password_hash` configured - it replies "no
  password_hash configured - run hashpw.py" and returns immediately
  (`admin_cmds.py:113-115`); there is nothing to check the password against.
- A bare `.auth` with no password (`admin_cmds.py:117-119`) replies the usage hint and
  returns; it is counted as neither a failure nor an audited event, which makes it the
  one way to confirm a `password_hash` is configured without consuming a lockout attempt.
- A supplied password longer than 128 characters is rejected before it ever reaches
  `verify_password` (`admin_cmds.py:120-122`).

The password is then checked with `verify_password` run in a worker thread
(`admin_cmds.py:148`).

Lockout state is `self._auth_fails: dict[str, tuple[int, float]]` keyed on lowercased
nick, guarded by `self._auth_lock`:

- 5 failures (`_AUTH_MAX_FAILS`) within a 300s window (`_AUTH_LOCKOUT`,
  `internets.py:170-172`) locks further attempts out for the remaining window
  (`admin_cmds.py:135-145`).
- The window is a **sliding** lockout: a refused attempt while locked rewrites
  `last_t = now` (`admin_cmds.py:140`), so an attacker trickling one guess per window
  can never let the lockout expire mid-attempt.
- The fail counter is re-read *inside* the lock immediately before incrementing
  (`admin_cmds.py:164-168`, `191-197`), because it was snapshotted before the
  `await asyncio.to_thread(verify_password, ...)` call; a second attempt racing in
  during that await must not have its failure silently dropped.
- `_auth_fails` is opportunistically pruned past `_AUTH_CLEANUP_THRESHOLD = 50` entries
  (`admin_cmds.py:127-131`), but the prune only discards entries whose last failure is
  older than `_AUTH_LOCKOUT` (300s) - it bounds long-term accumulation across many
  lockout windows, not burst growth. A flood of distinct attacker-controlled nicks, each
  making one attempt within the same 300s window, all stay in the dict at once; nothing
  in `cmd_auth` caps the dict size within a single window.

On a **correct** password, `cmd_auth` still refuses to create a session unless a live
hostmask is currently known for that nick (`admin_cmds.py:171-185`):

```
hostmask = self._nick_hosts.get(k)
if not hostmask or hostmask == "unknown":
    # Fail closed: never persist a binding we cannot later verify.
    ...
    self.preply(nick, reply_to,
        f"{nick}: can't confirm your hostmask right now - re-send the command.")
    log.warning("Auth refused for %s: no current hostmask to bind", nick)
    return
```

Why: the admin can quit mid-`verify_password` (the await point), which drops their
`_nick_hosts` entry. If `cmd_auth` persisted the `"unknown"` sentinel instead of refusing,
it would hand `is_admin` a session it can never re-verify against a live hostmask -
`is_admin`'s own fail-closed branch treats a stored `"unknown"` as an active revoke
(`internets.py:373-374`), so the two checks are redundant on purpose: `cmd_auth` refuses
to *create* an unverifiable binding, `is_admin` refuses to *honor* one if it ever got
created some other way. Do not "simplify" `cmd_auth` to fall back to the sentinel - that
reopens the nick-only-admin-outlives-disconnect hole documented at `internets.py:367-370`.

The password itself never appears in a log line or an audit record: `verify_password`'s
own `ValueError` (known config error) is logged with its message (safe - no password
content); any other backend exception is logged as `type(e).__name__` only
(`admin_cmds.py:157-163`), because argon2/bcrypt/scrypt backends can echo input or hash
fragments in their exception text. The audit record for a successful auth passes `None`
as `args` (`admin_cmds.py:189`).

Only the success path is audited. A failed attempt (`admin_cmds.py:199-200`) and a
lockout hit (`admin_cmds.py:142-145`) are both logged with `log.warning` only, never
through `_audit` - for a document tracking a security-facing command this is the wrong
way round: the record of who got in exists, the record of who tried and failed, or got
locked out, does not.

### `.rehash`: config reload, log-filter reset, and session clearing

`cmd_rehash` (`admin_cmds.py:434`) does three things in sequence, and the last one - the
admin-session clear - is **not unconditional**. Two earlier paths return before reaching
it, and existing sessions survive both:

- Config reload failure: `reload_config()` raises, the handler logs and replies "failed
  to read config", and returns (`admin_cmds.py:443-446`).
- An unrecognized `password_hash` prefix: after a successful reload, the new hash's
  prefix must split to exactly `scrypt`, `bcrypt`, or `argon2` before the first `$`
  (`admin_cmds.py:463-464`); anything else replies "Bad password_hash format" - logging
  only the prefix length, never the (possibly attacker-supplied) value
  (`admin_cmds.py:463-468`) - and returns. `tests/test_admin_cmds.py:621-625` exercises
  this path.

Only past both of those does it reach `self._authed.clear()` (`admin_cmds.py:470-472`),
deauthenticating every admin including the caller. Before that, the reload's level
name is resolved with a bare `lvl = getattr(logging, new_level, None)`
(`admin_cmds.py:449`) - there is no check against botlog's `VALID_LEVELS`, so any
truthy attribute name found on the `logging` module passes through, while the
legitimate level name `NOTSET` resolves to `0`, is falsy, and is silently skipped along
with the rest of this step. Only when `lvl` is truthy does `.rehash` reset the
log-filter base level, set `log_filter.global_debug = False`, and call
`clear_subsystems()` (`admin_cmds.py:451-453`), which wipes every per-subsystem debug
override set by prior `.debug`/`.loglevel` calls - not just the base level.

### `.loglevel`: argument order

The command's own usage string is `loglevel [LEVEL | <logger> LEVEL]`
(`botlog.py:303`). One bare argument sets the base level directly and also clears
`log_filter.global_debug` back to `False` (`botlog.py:273-280`) - the same
`global_debug` reset described for `.rehash` above, but triggered here by `.loglevel`
alone, with no config reload involved. Two arguments are `<logger> LEVEL`, in that
order - `target, level = args[0], args[1].upper()` (`botlog.py:282-283`) - and the logger name
must start with `"internets"` (`botlog.py:284-285`) or the call returns an error string
and `cmd_loglevel` never reaches the audit call (`admin_cmds.py:877-884`), so no audit
record is written for a rejected logger name. Three or more arguments fall through to
the same usage string, returned as an error (`botlog.py:303`) and replied as
`{nick}: usage: ...` - also never audited, for the same reason. No arguments prints the
current base level, global-debug flag, and active per-subsystem overrides
(`botlog.py:261-271`) and is also not audited (`admin_cmds.py:875-876` replies
directly, bypassing the `if parts:` audit branch).

### Shadow-ban persistence

`.shadow-ban` and `.shadow-unban` are not pure in-memory state: both flush the set to
the configured shadow-ban file (0600) via `await asyncio.to_thread(self._save_shadow_bans)`
(`admin_cmds.py:832`, `851`; writer `internets.py:428-453`), so bans survive a restart.
The path is `cfg["bot"]["shadow_bans_file"]`, defaulting to `shadow_bans.json`
(`internets.py:269-270`), not a hardcoded filename.
`IRCBot.__init__` loads the file back via `_load_shadow_bans()` (`internets.py:271`,
implementation `396-415`), tolerant of a missing or corrupt file - load failure just
leaves the set empty rather than blocking startup.

`cmd_shadow_ban` has two guards ahead of the add:

- "shadow-ban store not initialised" if `_shadow_bans` is absent from the instance at
  all (`admin_cmds.py:823-825`) - defensive, since the attribute is declared on
  `IRCBot.__init__` and not in the mixin's stub block (see the top of this section).
- A no-op with "is already shadow-banned" if the target is already in the set
  (`admin_cmds.py:826-828`), which also means a second `.shadow-ban` on an already-banned
  nick does not overwrite an existing reason.

Neither guard checks whether the target is itself an admin - only the bot's own nick and
the calling admin are refused (`admin_cmds.py:817-822`). Combined with the dispatch-side
drop running ahead of the flood limiter and `_require_admin` (`internets.py:617`), one
admin can shadow-ban another and silently lock them out of every command, `.deauth` and
`.shutdown` included, with the ban persisted to disk across a restart.

### `.audit`: argument grammar and failure modes

`cmd_audit` (`admin_cmds.py:665`) has three distinct failure replies before it gets to
formatting anything:

- Audit backend unavailable: constructing the audit object raises, replied as
  `audit log unavailable: {e!r}` (`admin_cmds.py:668-672`).
- Missing log file: reported as "audit log is empty (no records yet)"
  (`admin_cmds.py:674-676`) - indistinguishable from "no records were ever written" by
  design, since the log is only created on first record.
- Read failure once the file exists: an `OSError` during the read is reported as
  `type(e).__name__` only, never `str(e)` (`admin_cmds.py:708-710`), matching the same
  no-exception-text-to-IRC pattern used for `cmd_auth`'s backend errors.

Argument handling (`admin_cmds.py:678-702`): the bare-digit form clamps `N` to `1..200`
(`admin_cmds.py:687`); `grep <pattern>` widens the default tail window from 10 to 50
matches before filtering (`admin_cmds.py:683-685`); `tail` narrows it to 5
(`admin_cmds.py:689`); `verify` runs the HMAC-chain check and returns without touching
the tail logic at all (`admin_cmds.py:690-698`); any other first token prints the usage
string and returns without reading the file (`admin_cmds.py:699-702`).

Once past argument handling, `cmd_audit` reads the entire audit log file into memory in
one pass - `entries = [_audit_parse(line) for line in f if line.strip()]`
(`admin_cmds.py:704-707`) - before any tail or grep slicing runs. The comment at that
line ("audit log files are small - append-only admin ops") is the only thing keeping
this bounded; there is no size or line cap enforced in code.

### `.nick`: validation

The new nick must match an RFC-2812-shaped pattern capped at 30 characters (regex at
`admin_cmds.py:582`: first char a letter or one of the IRC special chars, then up to 29
more of letter/digit/special/hyphen) or the command refuses with "invalid nick". A
request for the nick the bot is already using is also refused, before anything is sent
to the server (`admin_cmds.py:586-588`).

### `.help admin`: hardcoded command list

The admin command grid shown by `.help admin` is a literal, hand-maintained list
(`admin_cmds.py:264-271`), not derived from `IRCBot._CORE` (the actual dispatch table).
It omits the `die` alias for `.shutdown` and can silently drift from the real command
set if a new admin `cmd_*` handler is added to `_CORE` without also updating this list.

### Module grouping and the default `.help` output

The no-arg `.help` output groups modules by category using `_MODULE_GROUPS`
(`admin_cmds.py:33-46`), a fixed tuple of `(label, module names)` pairs. Any loaded
module not named in any group falls into a "More" bucket instead of being dropped
(`admin_cmds.py:338-341`), so a newly added module still shows up in `.help` with zero
doc changes required; categorizing it into `_MODULE_GROUPS` is a cosmetic follow-up, not
a correctness requirement. This holds only for a module that is both configured and
declares at least one command: `cmd_help` skips any module whose `COMMANDS` dict is
empty before grouping ever runs (`admin_cmds.py:228-229`), and an unconfigured module is
folded into `visible` only when the caller is an admin - `visible = set(configured +
(hidden if admin else []))` (`admin_cmds.py:328`) - so it stays hidden from everyone
else.

### Module-level helpers

The command handlers above are backed by a set of module-level helper functions with no
`cmd_*` entry point of their own:

- `_wrap_list` (`admin_cmds.py:910`) - hanging-indent word wrap for the default `.help`
  module roster.
- `_help_grid` (`admin_cmds.py:935`) - fixed-column uppercase grid used by `.help all`
  and `.help admin`.
- `_humanize_delta` (`admin_cmds.py:957`) - compact duration formatting for `.uptime`,
  `.stats`, and `.fingerprint`'s "last seen" age (`admin_cmds.py:774`).
- `_read_rss_kb` (`admin_cmds.py:970`) - reads `/proc/self/status` for `.stats`'
  memory line; returns `None` on any non-POSIX platform or read failure, which is why
  the `.stats` table above renders "n/a" rather than a number in that case.
- `_audit_parse` (`admin_cmds.py:986`), `_audit_haystack` (`admin_cmds.py:996`), and
  `_audit_format` (`admin_cmds.py:1008`) - JSON-line parsing, grep-target flattening, and
  IRC-line rendering for `.audit`; `_audit_format` truncates the `args` field to 160
  characters (`admin_cmds.py:1022-1023`) purely for display compactness. This is
  unrelated to reply splitting, which is handled unconditionally for any outbound
  message by `_split_msg` (`internets.py:341-353`) once it exceeds `_MAX_BODY`
  (400 bytes, `internets.py:165`).
- `_state_file` (`admin_cmds.py:1027`) and `_read_json_dict` (`admin_cmds.py:1040`) -
  resolve a module's configured state-file path and load it as a JSON dict, defaulting
  and failing safe (`{}`) on any error. These two are the read path behind
  `.fingerprint`'s cross-module aggregation (`admin_cmds.py:767-768`, `781-782`,
  `792-793`) - the same functions that make `.fingerprint` privacy-sensitive (see below)
  are what let it read another module's on-disk state without that module's cooperation.
- `_count_audit_mentions` (`admin_cmds.py:1055-1083`) - walks the audit log counting a
  target nick as actor vs. as a substring of `args`; backs the "audit mentions" line in
  `.fingerprint`.

### Audit log split: what gets recorded and why

`_audit` (`admin_cmds.py:82-94`) wraps `audit_log.default().record(...)`, resolving the
actor's hostmask from `_nick_hosts` and swallowing every exception (audit failure must
never break the admin action it is trying to log). See section 6 for the HMAC-chain
mechanics.

19 of the 27 `cmd_*` handlers call `self._audit(...)` (27 async `cmd_*` methods total, 19
with a `self._audit(...)` call site, counted directly against `admin_cmds.py`). The
split is by **mutation, not by sensitivity**:

- Logged: everything that changes bot state, IRC-visible behavior, or the auth/session
  set - `auth`, `deauth` (only when a session actually existed), `load`, `unload`,
  `reload`, `reloadall`, `restart`, `rehash`, `mode`, `snomask`, `raw`, `say`, `act`,
  `nick`, `shadow-ban`, `shadow-unban`, `loglevel` (only when a change actually applied,
  `admin_cmds.py:880-884`), `debug`, `shutdown`.
- Not logged: `help`, `version`, `modules`, `uptime`, `stats`, `audit`, `shadow-list`,
  `fingerprint` - all pure reads with no side effect on bot or IRC state.

`.fingerprint` is the one worth flagging explicitly: it is read-only by the letter of
that rule, but it aggregates PII (hostmask, channel presence, `.seen`/`.tell`/`.notes`
data) about a *third party* who never consented to being looked up, and that lookup
itself leaves no trace in the audit log - only mentions of the target nick *elsewhere* in
the log are counted (`admin_cmds.py:798-800`, via `_count_audit_mentions`,
`admin_cmds.py:1055-1083`). There is no record of which admin ran `.fingerprint` on whom.
This is an honest gap, not a bug: closing it would mean auditing every read, including
`.stats`/`.uptime`, which carries no privacy content. If `.fingerprint` usage ever needs
accountability, it is the one read-only command that should move into the logged set.

### Reply path and output sanitization

Every handler replies via `self.preply(nick, reply_to, msg)` (declared as a stub at
`admin_cmds.py:66`, implemented `internets.py:338-339`), which calls `reply(...,
privileged=True)`: to a channel it sends a NOTICE to the caller rather than a channel
PRIVMSG (`internets.py:329-336`), so admin-command output never appears in the channel
itself. `.say`/`.act` are the deliberate exception - they call `self.privmsg(target,
text)` directly (`admin_cmds.py:553`, `569`) to put the admin's text onto the wire as the
bot's own public speech, which is the entire point of the command.

Sanitization is layered, and `admin_cmds.py` does **not** call `strip_ctrl`
(`modules/base.py:strip_ctrl`, section 8) anywhere - that sanitizer exists for
*third-party/upstream* text spliced into a reply. That holds for every command below
except `.fingerprint`, which renders `seen.json`'s `detail` field
(`admin_cmds.py:767-776`) - third-party text such as a PART/QUIT reason or a PRIVMSG
body, with no sanitization on this read path. It stays safe only because
`modules/seen.py:161` runs `strip_ctrl` on `detail` at write time, before it ever
reaches disk - not because of any property of the admin path itself. What actually runs
on the wire:

- `_split_target_and_text` (`admin_cmds.py:525-540`) decides whether the first token of
  `.say`/`.act`'s argument is a target or the start of the message text: a token -
  whether a channel sigil (`#`/`&`/`+`/`!`) or nick-shaped - counts as a target only if a
  second token follows it (the final gate is `looks_like_target and len(parts) > 1`,
  `admin_cmds.py:538`); a lone token with nothing after it is not a target, so the whole
  argument is treated as text and `target` falls back to `reply_to`
  (`admin_cmds.py:540`). So `.say #chan` with no message text does not target `#chan` -
  it speaks the literal string `#chan` into `reply_to`. This is the entire reason both
  `.say #chan hi` and `.say hi` work as the same command.
- Once a target is chosen, `.say`/`.act` each reject it if it contains a comma or a
  space (`admin_cmds.py:550-552`, `565-567`). This specifically blocks the IRC
  multi-target `PRIVMSG a,b,c` broadcast form - a single `.say` call can only ever speak
  to one target, never fan out to several at once.
- `.raw` rejects CR, LF, and NUL and caps the line at 510 bytes before calling `send`
  (`admin_cmds.py:512-517`) - this is a protocol-framing guard (one line, one command),
  not a content filter.
- `.say`/`.act`/`.mode`/`.nick` perform no IRC-formatting-code (`\x02`/`\x03`/...)
  stripping, so an admin's bold/color codes reach the channel intact - this is correct
  for `.say`/`.act` (an admin may want to format their message) and irrelevant for `.mode`
  and `.nick`.
- `.act` does not strip `\x01` from the admin's text before wrapping it as
  `\x01ACTION {text}\x01` (`admin_cmds.py:569`), so an embedded `\x01` in the admin's own
  input terminates the CTCP framing early.
- The universal transport backstop is in `sender.py:184`: every outbound line has `\r`,
  `\n`, and `\x00` stripped in `_write_line` immediately before it hits the socket,
  regardless of which command produced it. This makes `.raw`'s own CR/LF/NUL check
  (`admin_cmds.py:512-513`) a redundant guard rather than the only line of defense - even
  a future admin command that forgot the check could not inject a second protocol line
  through the Sender.
- `preply`/`privmsg`/`notice` split any message exceeding the wire body limit into
  multiple lines on UTF-8 boundaries (`_split_msg`, `internets.py:341-353`); this is a
  framing concern, not a security one.

Net effect: admin-command *output text* is trusted (it comes from an authenticated
admin), so what runs on this path is entirely protocol framing (no stray CR/LF splitting
one command into two, no oversized lines) rather than content filtering. Contrast with
section 8, where the sanitizer's job is filtering *untrusted* upstream text before it
reaches the same wire.

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
