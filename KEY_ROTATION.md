# Key & Credential Rotation Policy

This document specifies how often each long-lived secret used by the
Internets bot should be rotated, the mechanics of zero-downtime rotation
through `secret_store`, what to do on suspected compromise, and how the
password-hashing parameters in `hashpw.py` migrate over time.

It is intentionally short. The bot is a hobbyist IRC client, not a bank
— but the credentials it holds (Apple Developer signing key, weather API
keys, NickServ password, admin password hash) are real and worth
treating with care.

---

## 1. Rotation cadence

| Credential | Cadence | Notes |
|---|---|---|
| `password_hash` (admin) | annually | Also rotate on any suspected compromise, or after a contributor with shell access departs. |
| `nickserv_password` / `sasl_password` | annually | Network-side change first; update `secret_store` second. |
| `server_password`, `oper_password` | annually | Coordinate with the IRC server admin. |
| Weather provider API keys (OpenWeather, Tomorrow.io, WeatherAPI, etc.) | per-vendor — see below | Most allow on-demand rotation via their dashboard. |
| **Apple WeatherKit `.p8` signing key** | **every 6 months** | Apple recommends 6 months; the key remains valid for up to ~1 year before mandatory rotation. Calendar this. |
| `twitch_client_secret`, `youtube_key`, `brave_key`, etc. | annually, or on policy change | Twitch in particular has rotated its OAuth flow twice; recheck the API docs at each rotation. |
| `omdb_key`, `lastfm_key`, `steam_key` | every 2 years | Low-blast-radius read-only keys. |
| `meteomatics_password` | annually | Service account password, treat like any HTTP-basic credential. |

### Per-vendor API-key rotation shape

Most providers expose the same workflow. Generic procedure:

1. Log in to the vendor dashboard.
2. Create a new key (do NOT delete the old one yet).
3. `python -m secret_store set <name>_v2 --value <new-key>` (see §2).
4. Swap consumers to read `<name>_v2`, restart bot, verify the new key works on a live channel.
5. Rename `<name>_v2` → `<name>` (delete then set), restart, then delete the old key in the vendor dashboard.

For Apple WeatherKit specifically: the key file is `.p8` PKCS#8 PEM.
Drop the new file in alongside the old (`AuthKey_NEWID.p8`), point
`weatherkit_key_file` at the new path, restart, then `shred -u` the old
file. The `key_id` (`kid`) changes too — update `weatherkit_key_id`
together with the file path.

---

## 2. Zero-downtime rotation through `secret_store`

The bot's `secret_store` module supports atomic updates without
restart-loop. Use a `_v2` suffix for the parallel write so existing
consumers keep reading the old value until you cut over.

```bash
# 1. Stage the new value alongside the old.
python -m secret_store set weatherapi_key_v2 --backend keyring

# 2. Manually edit the consumer to point at the new key
#    (or, for keys read by name, just rename in step 3).
#    For weather API keys this is just a config.ini section.

# 3. Swap: delete the old, rename the new.
python -m secret_store delete weatherapi_key
python -m secret_store set weatherapi_key --value "$(python -m secret_store get weatherapi_key_v2 --reveal)"
python -m secret_store delete weatherapi_key_v2

# 4. Restart the bot (or send the admin "reload" command if supported).
# 5. Confirm: python -m secret_store list  → backend should show keyring/file.
```

For env-var deployments (`INTERNETS_<NAME>` in the unit file), edit the
unit and `systemctl daemon-reload && systemctl restart internets`. No
`_v2` suffix needed — env vars are atomic per-process.

---

## 3. Compromise response

If a credential is leaked (committed to git, screen-shared, posted in a
log paste, ...):

1. **Rotate immediately**, before doing anything else. The clock is the
   attacker's friend.
   - Vendor API keys: dashboard → revoke + issue new.
   - Apple `.p8`: dashboard → revoke key by `kid`, issue new key,
     update `weatherkit_key_id` AND `weatherkit_key_file`.
   - IRC NickServ / SASL: NickServ `SET PASSWORD` first, then update
     `secret_store`.
   - Admin `password_hash`: re-run `python hashpw.py --algo argon2`,
     replace `password_hash` in `config.ini`, restart the bot. All
     active admin sessions are invalidated on next auth check.
2. **Audit the log** (`internets.log`) for suspicious activity in the
   window from when the secret was first exposed to now:
   - Unexpected `admin` re-auth attempts (look for repeated
     `verify_password` failures).
   - Outbound IRC `PRIVMSG`/`NOTICE` to channels the bot doesn't
     normally use.
   - Unusual weather-provider rate-limit hits (a third party may be
     using your key).
3. **Force admin re-auth** by changing `password_hash` even if the admin
   password itself was not the leaked credential — a compromised
   secret store implies the hash may also be exfiltrated.
4. **Purge from git history** if the secret was committed:
   `git filter-repo --invert-paths --path <file>` then force-push, and
   tell any collaborators to re-clone. Note: assume the secret is
   already public once it has been pushed; rotation is mandatory.
5. **File the incident** in `AUDIT.md` with date, scope, and what was
   rotated. Future-you will appreciate it.

---

## 4. `hashpw.py` parameter migration

The stored hash format (`algo$rest`) carries its own parameters — N/r/p
for scrypt, the embedded cost+memory for argon2id, the rounds for
bcrypt. This is by design: **bumping the defaults in `hashpw.py` never
invalidates existing hashes.**

How parameter upgrades roll out:

1. Operator pulls the new bot version; `hashpw.py` now defaults to e.g.
   argon2id @ 256 MiB / t=4 instead of 128 MiB / t=3.
2. Existing `password_hash` in `config.ini` continues to verify
   correctly — `verify_password()` reads the algo + params from the
   stored string, not from the new defaults.
3. The operator picks a convenient maintenance window and re-runs
   `python hashpw.py --algo argon2`. The new hash uses the new
   defaults. They paste it into `config.ini` and restart.
4. The next auth check writes nothing back to disk — re-hashing is a
   manual, explicit step. No silent upgrades; no `needs_rehash`
   side-effects.

This means:

- **You may bump defaults aggressively in `hashpw.py`** without
  worrying about breaking deployed installs.
- **Operators on slow hardware can override via env vars**
  (`INTERNETS_ARGON2_MEM_MIB`, `INTERNETS_ARGON2_TIME`,
  `INTERNETS_BCRYPT_ROUNDS`) without touching source.
- **The self-test in `_self_test_argon2()` auto-degrades** if the
  configured params take >1 s per hash on the current host — protects
  login UX on small VMs without manual intervention.

scrypt has the same property — the `_best_scrypt_params()` probe
chooses the strongest cost the host's OpenSSL build accepts at hash
time, so an old hash made on a beefy host still verifies on a smaller
one (verification uses the params embedded in the hash, not the probe).

### Recommended algorithm preference

argon2id > scrypt > bcrypt.

- **argon2id**: memory-hard, side-channel resistant, OWASP 2024 first
  choice. Resists 2026 GPU/ASIC attackers far better than bcrypt.
- **scrypt**: memory-hard but older; still strong with N≥2¹⁷. Used as
  the historical default in this project for compatibility — do not
  break that without coordinating with deployed users.
- **bcrypt**: CPU-bound only; FPGA/ASIC attackers get a much bigger
  speedup against bcrypt than against scrypt or argon2. Keep cost ≥13.
