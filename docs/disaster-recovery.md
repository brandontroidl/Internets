# Disaster recovery

Recovering an Internets deployment from data loss or host loss: what the design
can actually promise, what must be backed up, how to restore it, and a drill you
can execute to prove the procedure works before you need it.

This is the counterpart to [operations.md](operations.md), which covers backup as
one item on a maintenance checklist. Here the subject is the whole recovery path,
including the parts that are uncomfortable: the objectives this design can and
cannot support, the state files that have no integrity checking at all, and the
fact that a recovery procedure nobody has executed is a claim, not a capability.

Security incidents are [incident-response.md](incident-response.md). A host
compromise is both documents: contain there, rebuild here.

| Subsystem this page touches | Reference |
|---|---|
| Store envelope, quarantine, flush loop | [internals/store.md](internals/store.md) |
| Audit chain and key sidecar | [internals/audit_log.md](internals/audit_log.md) |
| Secret tiers and file mode gate | [internals/secret_store.md](internals/secret_store.md) |
| Process lock | [internals/process_lock.md](internals/process_lock.md) |
| Deployment directory and permissions | [deployment.md](deployment.md) |

(dr-objectives)=
## Recovery objectives

**RPO and RTO are your decision, not the software's.** Nothing in this codebase
sets a target, and no configuration key expresses one. What follows is what the
design can support, so you can pick numbers it will actually meet.

### What bounds RPO

Three layers, and the outermost one dominates.

| Layer | Bound | Set by |
|---|---|---|
| Backup interval | Whatever you schedule | You. This is the real RPO |
| In-memory flush lag | 30 s (core), 60 s (`seen`), 0 (rest) | `store.py - _FLUSH_INTERVAL`, `modules/seen.py` |
| Filesystem writeback | OS default, typically 5-30 s | Your kernel and mount options |

**Flush lag.** `store.py - Store` loads each dataset once at startup, mutates in
memory, and a daemon thread writes dirty datasets every `_FLUSH_INTERVAL = 30`
seconds. A hard kill loses up to 30 seconds of location, channel, and
user-tracking mutations. A graceful shutdown does not: `graceful_shutdown()`
calls `Store.stop()`, which flushes before returning.

The module-owned stores differ, and the difference is worth knowing:

| Store | Write trigger | Loss window on a hard kill |
|---|---|---|
| `locations.json`, `channels.json`, `users.json` | 30 s periodic | up to 30 s |
| `seen.json` | 60 s periodic | up to 60 s |
| `tells.json`, `notes.json`, `steamids.json`, `reminders.json` | on mutation | none, modulo writeback |
| `shadow_bans.json` | on mutation | none, modulo writeback |
| `audit.log` | on every privileged action | none, modulo writeback |

**No `fsync`, anywhere.** Verified: the string appears exactly once in the
codebase, in a stale comment in `admin_cmds.py` claiming `record()` fsyncs. It
does not. Every writer here is `mkstemp` plus `os.replace`, which is atomic with
respect to *readers* - you never observe a half-written file - but says nothing
about durability. Until the kernel flushes its page cache, a "written" file can
still be lost to a power cut or a kernel panic. That is why the third row above
exists and why it is not a number this project controls.

The practical consequence: a clean `kill -INT` gives you an RPO of zero against
the last backup. A power loss gives you an RPO of "the last backup, plus
whatever the kernel had already committed", which you cannot know from inside
the process. Do not promise a sub-minute RPO on a bot that is not on a UPS.

**One-deep `.bak` is not a backup.** `store.py - Store._write()` copies the
current good file to `<name>.bak` immediately before each atomic replace. It
holds exactly one prior version, it is overwritten on the next flush, and it can
be as little as 30 seconds old. It is a corruption undo, not a recovery point.
Verified in the drill below: restoring `locations.json.bak` after a two-flush
sequence recovered the first flush's data and lost the second's.

### What bounds RTO

| Stage | Bound |
|---|---|
| Restoring files | Size of the deployment directory over your transport |
| Process start | Config parse plus autoload of your module list |
| First connect | Immediate; a failed attempt backs off 15 s, capped at 300 s |
| Channel rejoin | Up to 10 s waiting for NickServ confirmation, then JOIN |

`_backoff_jittered(attempt, base=15.0, cap=300.0)` governs reconnect, and
`_NICKSERV_WAIT_TICKS = 40` at `_NICKSERV_TICK = 0.25` puts a 10-second ceiling
on the pre-rejoin identification wait. A restart on a healthy network is
seconds; a restart into a network problem inherits that backoff.

Measure your own start-to-joined time during the [drill](#dr-drill) and write it
down. Do not adopt a number from this page - your autoload list, your host, and
your network are what set it.

**RTO is bounded below by a human.** There is no supervisor tree, no standby, no
replication. The bot is one process on one host. A failure that needs a restore
needs an operator. Design your target around that rather than around the
software.

(dr-inventory)=
## What must be backed up

The deployment directory is the unit ([deployment.md](deployment.md)), but two
things live outside it and are the classic omissions.

### Not regenerable: back these up

| Artifact | Why it cannot be rebuilt |
|---|---|
| `config.ini` | Holds `[secrets]` and `[admin] password_hash` |
| `config.local.ini` | The overlay, usually the real `password_hash` |
| `audit.log` **+** `audit.log.key` **+** `audit.log.*` | The chain is unverifiable without its key |
| `locations.json`, `channels.json`, `users.json` | User-supplied and observed data |
| `shadow_bans.json` | Moderation decisions |
| `seen.json`, `tells.json`, `notes.json`, `steamids.json`, `reminders.json` | Module-owned user data |
| The service unit and any `EnvironmentFile` | Holds `INTERNETS_*` secrets, and lives in `/etc` |
| The WeatherKit `.p8` key file | `weatherkit_key_file` stores a *path*; the file may be anywhere |

The last two are outside the deployment directory. A backup scoped to
`/srv/internets` misses both, and you discover it when the restored bot cannot
authenticate to IRC because the unit's `Environment=` lines are gone.

### Regenerable: do not bother

| Artifact | How to rebuild |
|---|---|
| The code | `git clone`, then check out the tag you were running |
| The virtualenv | `pip install -r requirements.txt` |
| `__pycache__/`, `.coverage`, `dist/`, `sbom.cdx.json` | Build artifacts |
| `internets.pid` | Created at startup |

:::{warning}
**Never restore `internets.pid`.** `process_lock.py - ProcessLock.acquire()`
refuses a lockfile written by a different hostname, because it cannot probe a
foreign PID. Restoring one onto a rebuilt host produces a refusal to start that
only a manual `rm` clears.
:::

Two artifacts sit in between:

- **`internets.log` and its rotations** are not needed to reconstitute the bot,
  but they hold nick-to-location pairs and announced URLs that `.forgetme`
  cannot reach ([known-issues.md](known-issues.md) items 4 and 15). Decide
  deliberately whether your backups retain them, because backing them up extends
  the retention of data your privacy commands cannot erase.
- **`*.json.corrupt.*`** files are evidence, not state. Keep them until the
  incident is closed, then delete them; do not restore them into a live
  deployment.

(dr-integrity)=
### Two classes of state file, and why it matters at restore time

This is the single most important distinction in this document.

**Class 1: the three `store.py` datasets.** `locations.json`, `channels.json`,
and `users.json` are written by `Store._write()` inside a v2 envelope:

```json
{
  "schema": 2,
  "checksum": "9be7e5a26c2d689ba9fb...",
  "data": { "alice": "90210" }
}
```

On read, `Store._read()` rejects the file if it is over 10 MiB, is not valid
JSON, declares an unknown schema, has a missing or mismatched checksum, or
unwraps to the wrong top-level type. A rejected file is **quarantined**:

```text
Store: locations.json unusable (JSONDecodeError('Extra data: line 8 column 2
(char 157)')) - quarantined to locations.json.corrupt.1786867818
```

`Store._quarantine()` renames the file aside, so the bad copy survives for
recovery and the dataset starts empty. Combined with the one-deep `.bak`, a
corrupt file in this class is a **detected, recoverable, and loud** event.

**Class 2: everything else.** The five module-owned JSON stores and
`shadow_bans.json` have no envelope, no checksum, no quarantine, and no `.bak`.
A corrupt file is caught by a bare `except`, the dataset starts empty, and **the
next save overwrites the only copy**:

| Store | On corrupt read |
|---|---|
| `seen.json` | `seen: failed to load <path>: <repr>` at WARNING, then empty |
| `tells.json` | `tell: failed to load <path>: <err>` at WARNING, then empty |
| `notes.json` | `notes: failed to load <path>: <err>` at WARNING, then empty |
| `reminders.json` | `remind: failed to read <path>: <err>` at WARNING, then empty |
| `shadow_bans.json` | `shadow_bans: load failed: <type>: <err>` at WARNING, then empty |
| `steamids.json` | **nothing at all** - the exception is swallowed silently |

`modules/steam.py - SteamModule.on_load()` catches the read failure with a bare
`except Exception: self._ids = {}` and logs no line. A corrupt `steamids.json`
is indistinguishable from an absent one, and the first `.regsteam` after that
writes the empty dict over it.

The operational rule that follows: **for class 2, your backup is the only copy**,
and a silent unbanning of everyone in `shadow_bans.json` or a silent wipe of
`steamids.json` is a possible outcome of any unclean write. Grep the log for the
five WARNING strings above as part of any post-restart check.

(dr-backup)=
## Backup procedure

### Order of operations

1. **Stop the bot, or accept the loss window.** A graceful stop flushes every
   dirty dataset. A hot copy is consistent per-file (writes are atomic) but not
   consistent *across* files, and misses up to 30 seconds of core state.

   ```bash
   kill -INT "$(cut -d'|' -f1 internets.pid)"
   # wait for internets.pid to disappear
   ```

2. **Copy with permissions preserved.** The state files are 0600 and hold PII;
   the bot log holds PII too. A naive `cp` or a `tar` extracted under a
   permissive umask widens them.

   ```bash
   tar --numeric-owner -czf "internets-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" \
       -C /srv internets
   ```

   `tar` preserves modes by default. `cp` does not unless you pass `-a`. `rsync`
   needs `-a` (or at least `-p`). Verify after the fact rather than assuming:

   ```bash
   tar -tvzf internets-*.tar.gz | awk '$1 !~ /^-rw-------/ && $1 ~ /^-/'
   ```

   Anything that prints is a file whose mode is looser than 0600. Expect the
   `.bak` files and the bot log to show up here - see the next warning.

3. **Include the two out-of-tree artifacts** from
   [the inventory](#dr-inventory): the service unit (and its `EnvironmentFile`)
   and the WeatherKit `.p8` if `weatherkit_key_file` is configured.

   ```bash
   grep -n 'weatherkit_key_file' config.ini
   systemctl cat internets > unit-backup.txt
   ```

:::{warning}
**Known defect (backup permissions).** `store.py - Store._write()` copies the
previous good file to `<name>.bak` with `Path.write_bytes` and never chmods it,
so on first creation it takes umask-default permissions. Verified live: a `.bak`
created under a 0022 umask is 0644 while the live file is 0600, and
`users.json.bak` therefore exposes the PII that `users.json` protects. Until
this is fixed, set `UMask=0077` in the service unit, keep the deployment
directory at 0700, and put `chmod 600 *.bak` in your pre-backup step.
[known-issues.md](known-issues.md) item 13.
:::

### Encryption at rest

The archive contains every credential the deployment holds, in plaintext, by
design - the bot must be able to send them on the wire, so they cannot be
hashed. An unencrypted backup of this directory is equivalent to an unencrypted
copy of `config.ini`.

Encrypt to a key that is not stored on the bot host, so that a host compromise
does not also yield the backups:

```bash
tar --numeric-owner -czf - -C /srv internets \
  | age -r age1... > "internets-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.age"
```

Or `gpg --encrypt --recipient ...`, or your backup tool's own repository
encryption. The mechanism matters less than the key location: **the decryption
key must not be readable by the bot user.** If it is, the backup adds no
protection over the live directory.

### Off-host storage

One host, one process, no replication. A backup on the same disk protects
against a bad write and nothing else. Push the archive to a destination the bot
host cannot delete from - append-only object storage, a pull-based backup server,
or offline media - because ransomware and a compromised bot user both reach
anything the bot host can write to.

Retention should exceed the window in which a silent corruption could go
unnoticed. For class 2 stores that window is unbounded in principle, because
`steamids.json` fails silently: keep enough generations that you can go back past
the last time you actually verified the data.

(dr-restore)=
## Restore procedures

Three shapes, from smallest to largest. The ordering constraints are the same in
all three and they are the part people get wrong:

1. **Process lock first.** The bot must be stopped and `internets.pid` gone
   before you touch any state file. Two writers against one directory is exactly
   what the lock exists to prevent, and restoring under a live bot means the next
   flush overwrites what you just put back.
2. **Permissions before secrets.** `secret_store.py - perms_ok()` requires
   `config.ini` to be **exactly** 0600 and fails closed otherwise, returning the
   default for every secret behind one error line.
3. **Secrets before start.** A bot started without its keys connects, logs one
   `REFUSING to read` line, and runs degraded rather than failing.
4. **Start last, and verify before declaring done.**

### A. Single corrupt state file

Symptom, in the log at ERROR:

```text
Store: users.json unusable (_StoreRejected('checksum mismatch')) - quarantined
to users.json.corrupt.1786867376
```

You have a deadline. After the quarantine the live file is **absent**. The first
flush that writes the dataset again creates a fresh file without touching `.bak`
(because `Store._write()` only writes a `.bak` when the live file exists), but
the **second** flush overwrites `.bak` with the post-quarantine, near-empty
content. On the 30-second flush interval that is roughly a minute. Stop the bot
before you investigate.

```bash
kill -INT "$(cut -d'|' -f1 internets.pid)"
ls -la users.json*
```

Then pick a source, in preference order:

```bash
# 1. Your backup - the only source that is not one flush from being lost
tar -xzf internets-BACKUP.tar.gz -C /tmp/restore internets/users.json

# 2. The one-deep .bak, if the bot has not flushed twice since
cp -a users.json.bak users.json

# 3. The quarantined file, if the corruption is repairable by hand
python -m json.tool users.json.corrupt.1786867376 | head
```

Validate before starting, not after. This reads the file exactly as the bot
would, without launching anything:

```bash
python -c "
import json
from store import _unwrap, _StoreRejected
for name, want in (('locations.json', dict), ('channels.json', list),
                   ('users.json', dict)):
    try:
        data = _unwrap(json.load(open(name)))
        assert type(data) is want, f'type {type(data).__name__}'
        print(f'{name:20} OK  ({len(data)} entries)')
    except (OSError, ValueError, AssertionError, _StoreRejected) as e:
        print(f'{name:20} REJECTED  {e!r}')
"
```

Expected:

```text
locations.json       OK  (2 entries)
channels.json        OK  (1 entries)
users.json           OK  (1 entries)
```

Fix permissions, then start:

```bash
chmod 600 *.json *.json.bak
```

For a **class 2** file - `seen.json`, `tells.json`, `notes.json`,
`steamids.json`, `reminders.json`, `shadow_bans.json` - there is no envelope to
validate and no `.bak` to fall back to. Your backup is the only source. Confirm
it is at least parseable:

```bash
python -m json.tool notes.json > /dev/null && echo OK
```

### B. Total state loss, host intact

Everything under the deployment directory is gone or untrusted, but the host and
the code are fine.

```bash
# 1. Confirm nothing is running
pgrep -af internets.py ; ls -la internets.pid

# 2. Restore into place, preserving modes
tar --numeric-owner -xzf internets-BACKUP.tar.gz -C /srv

# 3. Remove the one file that must not come back
rm -f /srv/internets/internets.pid

# 4. Permissions
cd /srv/internets
chmod 700 .
chmod 600 config.ini config.local.ini *.json *.json.bak audit.log audit.log.key

# 5. Confirm the secret store can read the file
python -m secret_store status
python -m secret_store list | grep -v '(unset)'
```

`status` must report `perms_ok True` and `secrets_file_perms 0o600`. If it
reports a mode instead, `secret_store.get()` will refuse every read.

:::{warning}
Do not follow the bot's own startup advice here. On a world-readable
`config.ini` `botlog.py` prints `config.ini is world-readable - consider: chmod
640 config.ini`, and 0640 fails `perms_ok()` closed. So does 0400. Use exactly
`chmod 600`. [known-issues.md](known-issues.md) item 7.
:::

Then start and verify per [After any restore](#dr-verify).

### C. Full host rebuild from scratch

```bash
# 1. Dedicated unprivileged user; INTERNETS_ALLOW_ROOT stays unset
useradd -r -m -d /srv/internets -s /usr/sbin/nologin internets

# 2. Code at the tag you were running
sudo -u internets git clone https://github.com/brandontroidl/Internets /srv/internets
cd /srv/internets && sudo -u internets git checkout <tag>

# 3. Dependencies.  Install from requirements.txt, not the lock -
#    see the lockfile defect below.
sudo -u internets python -m venv venv
sudo -u internets venv/bin/pip install -r requirements.txt

# 4. Restore data only.  Do NOT restore internets.pid.
sudo -u internets tar --numeric-owner -xzf BACKUP.tar.gz \
     -C /srv/internets --strip-components=1 \
     internets/config.ini internets/config.local.ini \
     internets/locations.json internets/channels.json internets/users.json \
     internets/shadow_bans.json internets/seen.json internets/tells.json \
     internets/notes.json internets/steamids.json internets/reminders.json \
     internets/audit.log internets/audit.log.key

# 5. Out-of-tree artifacts
#    - the service unit and its EnvironmentFile
#    - the WeatherKit .p8 at whatever weatherkit_key_file points to

# 6. Permissions
chmod 700 /srv/internets
chmod 600 /srv/internets/config.ini /srv/internets/*.json \
          /srv/internets/audit.log /srv/internets/audit.log.key

# 7. Start under the service manager, with WorkingDirectory set
systemctl start internets
```

:::{warning}
**Known defect (dependency lockfile).** `requirements.lock` was generated on
Python 3.14 rather than the 3.10 that `scripts/regen-lockfile.sh` mandates, so
it omits marker-gated transitives such as `typing_extensions>=4.4`. A
`pip install --require-hashes -r requirements.lock` fails on Python 3.10 through
3.12. During a rebuild that is exactly when you want hash-pinned installs to
work, so regenerate the lock per the script beforehand, or install from
`requirements.txt` and accept the weaker guarantee.
[known-issues.md](known-issues.md) item 6.
:::

If this rebuild follows a compromise rather than a hardware failure, do **not**
restore `config.ini`, `audit.log`, or `audit.log.key`. Build a fresh config from
`config.ini.example` and set every rotated secret by hand; see the rotation
matrix in [incident-response.md](incident-response.md#4-host-compromise).

(dr-verify)=
### After any restore

```text
.health
.audit verify
.stats
.modules
```

And in the log, within the first minute:

```text
event=sasl_success nick=internets
event=rejoin nickserv=confirmed
Rejoining #ops
```

Grep for the failures a restore actually produces:

```bash
grep -nE 'Store: .* unusable|failed to load|failed to read|load failed|REFUSING to read' \
     internets.log
```

Any hit means a state file did not survive. Stop, and go back to
[procedure A](#a-single-corrupt-state-file) before the next flush overwrites it.

(dr-audit)=
## Audit chain continuity across a restore

The audit chain is a per-file linked list, not a global ledger. Three properties
decide what a restore does to it:

- `AuditLog.verify()` walks from a genesis of 64 zeros to the end of the **live**
  `audit.log`. It never reads rotated segments.
- Rotation renames the log and starts a fresh chain, but does **not** rotate
  `audit.log.key`. One key covers every segment written since that key existed.
- `AuditLog.record()` restores its tip by reading the last `this_hash` in the
  file, so appending to a restored log continues that chain correctly.

What that means in practice:

| You restore | Result |
|---|---|
| `audit.log` **and** its key, together | Chain verifies; new records append cleanly |
| `audit.log` without the key | A fresh key is generated; **every prior record fails verification** |
| The key without the log | New chain from genesis; old segments still verifiable |
| Neither | Fresh key, fresh chain from genesis |

The second row is the trap, and it is silent: the bot starts, generates a new
32-byte key at 0600 on the first `record()`, and from then on `.audit verify`
reports the chain broken at index 0. That is not tampering; it is a restore that
dropped the sidecar. Back them up together, always.

### What a gap means

A restore usually leaves a hole between the last record in the backup and the
first record after the restore. Nothing in the bot marks it. `verify()` reports
**intact** across the gap, because the chain links the restored tail to the new
records correctly - the missing records simply never existed as far as the file
is concerned.

So a gap is invisible to verification and must be reconstructed from timestamps:

```bash
python -c "
import json
prev = None
for i, l in enumerate(open('audit.log')):
    r = json.loads(l)
    if prev and r['ts'][:10] != prev[:10]:
        print(f'index {i}: {prev} -> {r[\"ts\"]}')
    prev = r['ts']
"
```

Compare the first post-restore timestamp against the last pre-restore one, and
against when you know the bot was down. If the gap is longer than the outage,
records were lost that the outage does not explain, and that is a finding for
[incident-response.md](incident-response.md#6-audit-log-tampering-suspected).

Two further honest limits:

- **Tail truncation is undetectable.** Removing records from the end leaves a
  chain that verifies. A restore from a backup taken before some privileged
  actions is functionally identical to a truncation, and neither is visible.
- **Verification can be downgraded.** Records without a `v` field are verified
  with unkeyed SHA-256, at any position, so an intact verdict is evidence
  against accidental corruption and not against a deliberate writer.
  [known-issues.md](known-issues.md) item 5.

Record the restore boundary in your own notes - date, what was restored, and the
audit record count on both sides. That note is the only thing that will tell a
future reader the gap was a restore rather than a deletion.

(dr-drill)=
## Restore drill

**A recovery path that has never been executed is unverified.** Possessing a
backup is not being able to restore it. Run this in a scratch directory, from a
real backup, at least once per quarter, and after any change to the backup
tooling.

The expected output below is real: every line was produced by running these
snippets against a synthetic deployment. Your entry counts and timestamps will
differ; the shapes should not.

### Setup

```bash
mkdir -p /tmp/dr-drill && cd /tmp/dr-drill
tar --numeric-owner -xzf /path/to/internets-BACKUP.tar.gz --strip-components=1
export PYTHONPATH=/srv/internets      # the checkout, for the store/audit imports
```

### Step 1: what came out of the archive

```bash
ls -la
```

```text
-rw-------. 1 btroidl btroidl 558 Aug 16 01:10 audit.log
-rw-------. 1 btroidl btroidl  64 Aug 16 01:10 audit.log.key
-rw-------. 1 btroidl btroidl 127 Aug 16 01:10 channels.json
-rw-------. 1 btroidl btroidl 157 Aug 16 01:10 locations.json
-rw-r--r--. 1 btroidl btroidl 137 Aug 16 01:10 locations.json.bak
-rw-------. 1 btroidl btroidl 375 Aug 16 01:10 users.json
```

**Check:** `audit.log.key` is present. `config.ini` is present. The `.bak` at
0644 is the known permissions defect, and its appearance here is expected, not a
drill failure.

### Step 2: validate the three checksummed datasets

```bash
python3 -c "
import json
from store import _unwrap, _StoreRejected
for name, want in (('locations.json', dict), ('channels.json', list),
                   ('users.json', dict)):
    try:
        data = _unwrap(json.load(open(name)))
        assert type(data) is want, f'type {type(data).__name__}'
        print(f'{name:20} OK  ({len(data)} entries)')
    except (OSError, ValueError, AssertionError, _StoreRejected) as e:
        print(f'{name:20} REJECTED  {e!r}')
"
```

```text
locations.json       OK  (2 entries)
channels.json        OK  (1 entries)
users.json           OK  (1 entries)
```

**Check:** three `OK` lines, and entry counts in the range you expect. A count
of zero on `users.json` means the backup captured an empty dataset, which is a
backup failure that would otherwise surface only after a real restore.

Then the class 2 files, which have no envelope to check - parseability is all
you get:

```bash
for f in seen.json tells.json notes.json steamids.json reminders.json \
         shadow_bans.json; do
  [ -f "$f" ] && { python3 -m json.tool "$f" > /dev/null \
     && echo "$f OK" || echo "$f UNPARSEABLE"; }
done
```

### Step 3: permissions survived the round trip

```bash
find . -maxdepth 1 -type f -printf '%m %n %f\n' | sort -k3
```

```text
600 1 audit.log
600 1 audit.log.key
600 1 channels.json
600 1 locations.json
644 1 locations.json.bak
600 1 users.json
```

**Check:** everything except `*.bak` is 600. A 644 on `users.json` or
`config.ini` means your archive tool or extraction umask widened it, and that is
the single most common silent backup defect.

### Step 4: the audit chain, including rotated segments

```bash
python3 -c "
from pathlib import Path
from audit_log import AuditLog
for p in [Path('audit.log')] + sorted(Path('.').glob('audit.log.2*')):
    a = AuditLog(p)
    ok, idx = a.verify()
    print(f'{p.name:28} {\"intact\" if ok else f\"BROKEN at {idx}\"} '
          f'({a.count()} records)')
"
```

```text
audit.log                    intact (2 records)
```

**Check:** intact, and the rotated segments are listed. The bot's own
`.audit verify` never reads them, so this loop is the only place they get
checked. A `BROKEN at 0` on every file at once means the key did not come out of
the archive.

### Step 5: prove the corruption path actually recovers

This is the step that distinguishes a drill from a file listing. It deliberately
breaks a state file and recovers it.

```bash
cp locations.json locations.json.pristine
printf 'x' >> locations.json          # append one byte: valid file, invalid JSON

python3 -c "
from store import Store
s = Store('locations.json', 'channels.json', 'users.json')
print('loaded locations:', s._locs)
s.stop()
"
```

```text
Store: locations.json unusable (JSONDecodeError('Extra data: line 8 column 2
(char 157)')) - quarantined to locations.json.corrupt.1786867818
loaded locations: {}
```

**Check:** the quarantine fired, the dataset started empty, and the bad file was
preserved rather than deleted.

```bash
ls -1
```

```text
audit.log
audit.log.key
channels.json
locations.json.bak
locations.json.corrupt.1786867818
locations.json.pristine
users.json
```

**Check:** `locations.json` is **gone** - quarantine renames rather than copies.
This is the state in which a running bot is one flush away from recreating an
empty file, and two flushes away from overwriting `.bak`.

Now recover, first from the `.bak`:

```bash
cp locations.json.bak locations.json
python3 -c "
from store import Store
s = Store('locations.json', 'channels.json', 'users.json')
print('loaded locations:', s._locs)
s.stop()
"
```

```text
loaded locations: {'alice': '90210'}
```

Then from the pristine copy, standing in for your backup:

```bash
cp locations.json.pristine locations.json
python3 -c "
from store import Store
s = Store('locations.json', 'channels.json', 'users.json')
print('loaded locations:', s._locs)
s.stop()
"
```

```text
loaded locations: {'alice': '90210', 'bob': '10001'}
```

**Check, and this is the point of the whole step:** the `.bak` recovered
`alice` but **not** `bob`. The synthetic deployment flushed twice, and the `.bak`
holds the state as of the first flush. That is the one-deep backup's real
behaviour, measured rather than assumed, and it is why `.bak` is a corruption
undo and your archive is the recovery point.

### Step 6: start the bot against the drill directory

The steps above prove the data. This proves the deployment.

```bash
cd /tmp/dr-drill
# Edit config.ini: a test nick, a test channel, metrics disabled.
python3 /srv/internets/internets.py --no-console --loglevel INFO
```

Watch for, in order:

```text
process_lock: acquired /tmp/dr-drill/internets.pid (pid=12345)
event=connect_begin host=irc.example.net port=6697 ssl=True verify=True
event=sasl_success nick=internets-drill
event=rejoin nickserv=confirmed
Rejoining #drill
```

**Check:** it reached the channel list from the restored `channels.json`. Time
this from launch to the `Rejoining` line - that number is your measured RTO
contribution for the process, and it is the one you should put in your recovery
plan.

Then stop it, and clean up:

```bash
kill -INT <pid>
cd / && rm -rf /tmp/dr-drill
```

:::{warning}
Use a **test nick and a test channel**. The drill config points at real
credentials from your backup, so a drill run with the production nick will
collide with the live bot at services, and a drill run against production
channels will duplicate every response. Never run the drill against the
production deployment directory: it takes the process lock and mutates the state
files you are trying to protect.
:::

### Drill sign-off

Record, per run: the date, the backup archive you restored, the entry counts
from step 2, the audit record count from step 4, the measured time from step 6,
and anything that did not match the expected output. A drill whose result is not
written down does not carry forward to the next operator.

(dr-rollback)=
## Rollback: what breaks going backwards

Restoring the previous *code* is covered in
[deployment.md](deployment.md#rollback). The data-side hazards are here, because
a rollback is a recovery operation and it can lose state that a forward restore
would not.

### The state file format is not the hazard

`store.py - Store._read()` has accepted both the v2 checksum envelope and a
legacy v1 bare payload since the initial commit, and `_unwrap()` returns a v1
payload unchanged so the next flush re-wraps it. An older binary reading a v2
file is fine.

### The handling of a rejected file is the hazard

:::{warning}
**Quarantine and the one-deep `.bak` both arrived in 4.0.0**, in the same
CHANGELOG entry: "Store quarantine instead of clobber", under
`## [4.0.0] - 2026-06-28`. That entry states the prior behaviour explicitly -
`Store._read` reset to empty on a checksum, size, shape, or parse failure and
the next flush overwrote the only on-disk copy via `os.replace`, destroying
locations, channel-rejoin state, and opt-out flags.

So a rollback to **below 4.0.0** turns one bad read into permanent loss, with a
log line and nothing on disk. Copy the whole deployment directory aside before
the rollback, and treat that copy as the only recovery path.

Note that [deployment.md](deployment.md#rollback) currently attributes
quarantine to 5.0.0. The CHANGELOG places it in the 4.0.0 section; 5.0.0's
storage-related entry is the bcrypt 72-byte change, not this one.
:::

Two consequences of rolling back below 4.0.0:

- **No `.corrupt.*` files will appear.** Their absence is not evidence that
  nothing went wrong; the older code had no quarantine step.
- **No `.bak` either.** The one-deep backup was introduced by the same change,
  so below 4.0.0 there is no in-tree fallback at all and your archive is the
  only copy.

Between 4.0.0 and current, both mechanisms are present and a rollback in that
range does not change the corruption-handling behaviour.

### What else moves backwards badly

```{tabularcolumns} |p{0.30\linewidth}|p{0.62\linewidth}|
```

| Going backwards | Effect |
|---|---|
| Below 4.0.0 | Rejected state files are reset and clobbered; no quarantine, no `.bak` |
| Below 5.0.0, bcrypt password over 72 bytes | The direction is asymmetric: 5.0.0 refuses a bcrypt candidate over 72 bytes at verify time, older versions truncate and accept it. A long password that works before the rollback keeps working after it, and stops working when you roll forward again |
| Any version, dependencies left forward | An older binary against newer libraries is not the version you tested; reinstall from that version's `requirements.txt` |

The bcrypt case has its own log line, at WARNING from `hashpw`:

```text
bcrypt candidate exceeds the 72-byte limit - refusing. If this is the real
password, re-generate the hash with hashpw.py (argon2 has no such limit).
```

The user-visible symptom is only `.auth` answering `wrong password.`, so check
the log before assuming the password itself is wrong.

The audit log does **not** roll back. `record()` appends to one chain that
`verify()` walks from genesis with no version-scoped boundary, so an older
binary simply continues the existing chain. Records written by the newer version
remain valid: the `v: 2` HMAC scheme arrived in 3.0.0, so any binary you would
plausibly roll back to already verifies it.

### Rollback checklist

- [ ] Graceful stop; `internets.pid` gone.
- [ ] Whole deployment directory copied aside, modes preserved.
- [ ] Target version's `requirements.txt` installed, not the current one's.
- [ ] Started, then the log checked for `Store: <path> unusable` **before**
      assuming state survived.
- [ ] `.audit verify` intact and the record count continuous.
- [ ] If rolling back below 4.0.0: state files restored from the copy taken
      above, rather than left for the older binary to rewrite.

## Cross-references

- [operations.md](operations.md) - routine backup and the maintenance checklist.
- [deployment.md](deployment.md) - the deployment directory, permissions, and
  code-level rollback.
- [incident-response.md](incident-response.md) - recovering from a compromise
  rather than a failure.
- [state-and-persistence.md](state-and-persistence.md) - what each dataset holds
  and why.
- [known-issues.md](known-issues.md) - items 5, 6, 7, 12, 13, and 15 all bear on
  this page.
