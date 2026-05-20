# Setup & deployment guide

End-to-end checklist for taking the bot from a fresh clone to a hardened,
running deployment.  See `README.md` for feature documentation and
`THREAT_MODEL.md` / `PRIVACY.md` / `KEY_ROTATION.md` for the security
posture.

---

## 1. Local install

```bash
git clone https://github.com/brandontroidl/Internets.git
cd Internets
python -m venv .venv
source .venv/bin/activate
pip install -e ".[all,dev]"        # all = aiohttp + bcrypt + argon2 + PyJWT + keyring
```

Python 3.10+ required. Single hard dependency is `requests`; the rest are optional extras.

## 2. Credentials

```bash
python -m secret_store init        # creates secrets.ini from secrets.ini.example
                                   # with 0600 perms; comments preserved
$EDITOR secrets.ini                # paste your real keys
python -m secret_store status      # confirm backend
python -m secret_store list        # confirm each key reads back
```

The keys you'll most often want set:

| Key | What it does |
|-----|--------------|
| `nickserv_password` | NickServ / SASL identify |
| `weather_user_agent` | HTTP `User-Agent` for geocoding + weather APIs |
| `weatherapi_key` / `openweathermap_key` / etc. | one or more weather providers |
| `youtube_key` / `lastfm_key` / `omdb_key` / etc. | per-module APIs |

NWS (US gov) and Open-Meteo work without keys, so the bot always has weather
coverage even with zero API keys configured.

`get` is non-revealing by default — `python -m secret_store get nickserv_password`
prints `(set, 13 chars, backend=file)`, never the value.  Pass `--reveal` only
when you need to extract it (e.g. for rotation).

## 3. Personal non-secret settings

```bash
$EDITOR config.local.ini
```

Minimum:

```ini
[irc]
server = irc.your-network.org
nickname = MyBot

[admin]
; Generate with:  python hashpw.py --algo argon2
password_hash = argon2$$argon2id$v=19$...
```

`config.local.ini` is gitignored. It overlays `config.ini` (the committed template).
Per-user overrides also accepted via env vars (`INTERNETS_<NAME_UPPER>`).

## 4. Run the bot

Interactive (with console):

```bash
python internets.py
```

Daemon / systemd (no console):

```bash
python internets.py --no-console
```

The bot will refuse to start a second instance — a PID lockfile at
`./internets.pid` is acquired before any state-file writes.

## 5. Systemd unit (suggested)

```ini
# /etc/systemd/system/internets-bot.service
[Unit]
Description=Internets IRC bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
Group=botuser
WorkingDirectory=/home/botuser/Internets
ExecStart=/home/botuser/Internets/.venv/bin/python internets.py --no-console
Restart=on-failure
RestartSec=15

# Hardening (recommended)
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
ReadWritePaths=/home/botuser/Internets

# State files + secrets stay readable only to botuser
UMask=0077

# Optional: load creds via systemd LoadCredential / EnvironmentFile rather
# than secrets.ini, for TPM-backed encryption-at-rest on systemd 250+.
# LoadCredentialEncrypted=nickserv_password:/etc/credstore.encrypted/nickserv_password

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now internets-bot.service
sudo systemctl status internets-bot.service
journalctl -u internets-bot.service -f
```

## 6. GitHub repo settings

After the first push:

| Where | Setting | Why |
|-------|---------|-----|
| Settings → Security → Code security & analysis | Enable **Code scanning** ("Default setup" or "Other tools") | Lets `.github/workflows/security.yml` upload Bandit + pip-audit SARIF into the Security tab |
| Settings → Security → Code security & analysis | Enable **Dependabot alerts** + **Security updates** | Auto-PRs for vulnerable deps (config is at `.github/dependabot.yml`) |
| Settings → Security → Private vulnerability reporting | **Enable** | Gives researchers a private channel (`SECURITY.md` points reporters here) |
| Settings → Branches → main | **Branch protection rule**: require PR, require status checks (`tests`, `bandit`, `pip-audit`), require signed commits | Stops accidental direct pushes |
| Settings → Actions → General | **Workflow permissions**: read-only by default; allow `GITHUB_TOKEN` write only where needed | Least-privilege CI |
| `CODEOWNERS` (already committed) | Confirm `@brandontroidl` matches your GitHub handle | Auto-request review on PRs |
| If repo is **private** | Add `GITLEAKS_LICENSE` org secret | gitleaks-action needs it on private repos (free on public) |

For the GitHub CLI (alternative to web UI):

```bash
gh repo edit --enable-vulnerability-alerts --enable-automated-security-fixes
gh secret set GITLEAKS_LICENSE        # only if repo is private
```

## 7. Backups

State files that should be backed up (each 0600, gitignored):

```
locations.json       user → location mappings
channels.json        which channels the bot is invited to
users.json           per-channel user-tracking (PII — see PRIVACY.md)
audit.log            tamper-evident admin-action log
secrets.ini          credentials (keep this off-host; encrypt at rest)
config.local.ini     personal non-secret settings including admin hash
```

Quick rsync cron (encrypted destination recommended for `secrets.ini`):

```bash
0 4 * * * cd /home/botuser/Internets && tar czf /backup/internets-$(date +\%Y\%m\%d).tar.gz \
    locations.json channels.json users.json audit.log config.local.ini
# secrets.ini: encrypt separately with age/gpg before sending off-host
```

Verify integrity: `audit.log` is a hash-chained append-only log; check with
`python -c "from audit_log import default; print(default().verify())"` →
should print `(True, -1)`.

## 8. Optional: Prometheus metrics

```ini
# config.local.ini
[metrics]
enable = true
host = 127.0.0.1
port = 9779
```

Then `curl http://127.0.0.1:9779/metrics`.  **Never bind to 0.0.0.0** — the
endpoint is unauthenticated.  Scrape from a local Prometheus or via SSH
port-forward.

## 9. Smoke test

```bash
python -m py_compile $(find . -name '*.py' -not -path './.venv/*' -not -path '*/__pycache__/*')
python tests/run_tests.py            # should print "154 passed, 0 failed"
pytest -q tests/                     # should print "221 passed"
python -m secret_store status        # should show your chosen backend
python hashpw.py --algo argon2       # generate a test hash, confirm verify works
```

## 10. After deploy

- Monitor `internets.log` for `event=tls_unverified` (you set `ssl_verify=false`),
  `event=dispatch_rejected` (queue saturated), `event=audit_log_record_failed`.
- Run `python -m secret_store list` periodically to confirm nothing has
  silently regressed to a placeholder value.
- Review `PRIVACY.md` whenever you add a module that touches user data.
- Review `KEY_ROTATION.md` annually and rotate accordingly.
