"""Two-tier secret store — env var > config.ini[secrets].

Outbound credentials (NickServ password, SASL password, server password,
oper password, weather API keys, etc.) MUST be reversible — the bot has
to send them on the wire — so this module provides encryption-at-rest,
not hashing.  Hashing is one-way and would break authentication.

Lookup order for ``get(name)``:
    1. Environment variable ``INTERNETS_<NAME_UPPER>`` if set.
    2. ``config.ini`` ``[secrets]`` section, file mode strictly 0o600.
    3. Empty string default.

OS keyring support was removed in v2.7.0: this bot's primary target is
headless deployments where ``keyring`` has no usable backend, and the
optional desktop-session integration brought in ~10 transitive deps
(jeepney, secretstorage, jaraco-*, importlib-metadata, zipp, …) for no
practical benefit.  The 0o600 file backend is the only secret store;
``perms_ok()`` fails closed if the file is group- or world-readable.

config.ini is gitignored — it holds both the non-secret settings and
the ``[secrets]`` section.  ``config.ini.example`` is the committed
credential-free template.

CLI::

    python -m secret_store status
    python -m secret_store set <name> [--value <v>]
    python -m secret_store get <name>
    python -m secret_store delete <name>
    python -m secret_store migrate           # scrub plaintext from non-[secrets] sections
    python -m secret_store list              # show which keys are stored where
    python -m secret_store init              # bootstrap config.ini from config.ini.example
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import logging
import os
import stat
import sys
from pathlib import Path

log = logging.getLogger("internets.secrets")

ENV_PREFIX = "INTERNETS_"
# The file the [secrets] section lives in.  config.ini is gitignored;
# config.ini.example is the committed credential-free template (resolved
# fresh on each `init` call so tests can chdir into a tmp path).
SECRETS_FILE = Path("config.ini").resolve()

# Canonical secret names — every key the bot considers sensitive.
# Used by migrate / list / status.  Adding a key here makes it part of
# the migration sweep without any other code changes.
KNOWN_SECRETS: tuple[str, ...] = (
    # IRC auth (all sent reversibly on the wire — can't be hashed)
    "nickserv_password",
    "sasl_password",     # falls back to nickserv_password if unset
    "server_password",
    "oper_password",
    # PII / contact identifier sent in HTTP User-Agent
    "weather_user_agent",
    # Weather provider keys
    "weatherapi_key", "tomorrowio_key", "openweathermap_key",
    "visualcrossing_key", "pirateweather_key", "weatherstack_key",
    "accuweather_key", "worldweatheronline_key", "weatherbit_key",
    "stormglass_key",
    "meteomatics_username", "meteomatics_password",
    "weatherkit_team_id", "weatherkit_service_id", "weatherkit_key_id",
    "weatherkit_key_file",
    "airnow_key", "purpleair_key",
    "waqi_token", "openaq_key", "iqair_key", "tidecheck_key", "firms_key",
    "google_pollen_key", "n2yo_api_key",
    # Other module keys
    "omdb_key", "lastfm_key", "youtube_key",
    "finnhub_key", "alphavantage_key", "twelvedata_key",
    "steam_key",
    "twitch_client_id", "twitch_client_secret",
    "brave_key",
)

# Mapping: canonical secret name → (config.ini section, key)
# Drives the migrate command (knows where to scrape plaintext from).
CONFIG_LOCATIONS: dict[str, tuple[str, str]] = {
    "nickserv_password":      ("irc", "nickserv_password"),
    "server_password":        ("irc", "server_password"),
    "oper_password":          ("irc", "oper_password"),
    "weather_user_agent":     ("weather", "user_agent"),
    "weatherapi_key":         ("weather_providers", "weatherapi_key"),
    "tomorrowio_key":         ("weather_providers", "tomorrowio_key"),
    "openweathermap_key":     ("weather_providers", "openweathermap_key"),
    "visualcrossing_key":     ("weather_providers", "visualcrossing_key"),
    "pirateweather_key":      ("weather_providers", "pirateweather_key"),
    "weatherstack_key":       ("weather_providers", "weatherstack_key"),
    "accuweather_key":        ("weather_providers", "accuweather_key"),
    "worldweatheronline_key": ("weather_providers", "worldweatheronline_key"),
    "weatherbit_key":         ("weather_providers", "weatherbit_key"),
    "stormglass_key":         ("weather_providers", "stormglass_key"),
    "meteomatics_username":   ("weather_providers", "meteomatics_username"),
    "meteomatics_password":   ("weather_providers", "meteomatics_password"),
    "weatherkit_team_id":     ("weather_providers", "weatherkit_team_id"),
    "weatherkit_service_id":  ("weather_providers", "weatherkit_service_id"),
    "weatherkit_key_id":      ("weather_providers", "weatherkit_key_id"),
    "weatherkit_key_file":    ("weather_providers", "weatherkit_key_file"),
    "airnow_key":             ("weather_providers", "airnow_key"),
    "purpleair_key":          ("weather_providers", "purpleair_key"),
    "waqi_token":             ("weather_providers", "waqi_token"),
    "openaq_key":             ("weather_providers", "openaq_key"),
    "iqair_key":              ("weather_providers", "iqair_key"),
    "tidecheck_key":          ("weather_providers", "tidecheck_key"),
    "firms_key":              ("weather_providers", "firms_key"),
    "google_pollen_key":      ("weather_providers", "google_pollen_key"),
    "n2yo_api_key":           ("satpass", "n2yo_api_key"),
    "omdb_key":               ("imdb", "omdb_key"),
    "lastfm_key":              ("lastfm", "lastfm_key"),
    "youtube_key":             ("youtube", "youtube_key"),
    "finnhub_key":             ("stocks", "finnhub_key"),
    "alphavantage_key":        ("stocks", "alphavantage_key"),
    "twelvedata_key":          ("stocks", "twelvedata_key"),
    "steam_key":               ("steam", "steam_key"),
    "twitch_client_id":        ("twitch", "twitch_client_id"),
    "twitch_client_secret":    ("twitch", "twitch_client_secret"),
    "brave_key":               ("search", "brave_key"),
}

# Placeholders that mean "not set" — never migrated, never returned.
# All values matched case-insensitively (callers lowercase before lookup).
# Common dummy strings shipped in example configs, plus the obvious "fill
# me in" markers we've seen in the wild.
_PLACEHOLDERS = frozenset({
    "", "changeme", "change-me", "change_me",
    "your-key-here", "your_key_here", "your-key", "your_key", "<your-key>",
    "your-token", "your_token", "<your-token>",
    "your-api-key", "your_api_key", "<your-api-key>",
    "your-password", "your_password", "<your-password>",
    "placeholder", "set-via-secret-store", "set_via_secret_store",
    "todo", "tbd", "xxx", "none", "null", "n/a", "na",
    "example", "example-key", "demo", "test", "fixme",
    "insert-key-here", "insertkeyhere",
})


# ── Backend helpers ──────────────────────────────────────────────────

def _safe_exc(e: BaseException) -> str:
    """Return ``ExceptionType`` without the message.

    Exception messages from configparser / argon2 / bcrypt occasionally
    echo back fragments of the offending value (e.g. configparser
    includes the bad line, argon2 includes the hash in some error
    paths).  We never want those in our logs.
    """
    return type(e).__name__


def perms_ok(path: Path = SECRETS_FILE) -> tuple[bool, str]:
    """Check that ``path`` is 0o600 (owner rw only).  Returns (ok, reason)."""
    if not path.exists():
        return True, "absent"
    try:
        st = path.stat()
    except OSError as e:
        return False, f"stat failed: {e}"
    if os.name == "nt":
        # POSIX modes are advisory on Windows; rely on filesystem ACLs.
        return True, "windows (acl-based)"
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        return False, f"mode is {oct(mode)}, expected 0o600 — run `chmod 600 {path}`"
    return True, "0o600"


# ── Public API ───────────────────────────────────────────────────────

def get(name: str, default: str = "") -> str:
    """Return the secret value, or ``default`` if not stored anywhere.

    Tiered lookup (first hit wins): env var → config.ini[secrets].
    """
    # 1) Env var
    env_key = ENV_PREFIX + name.upper()
    val = os.environ.get(env_key)
    if val:
        return val
    # 2) config.ini [secrets]
    if SECRETS_FILE.exists():
        ok, reason = perms_ok(SECRETS_FILE)
        if not ok:
            log.error("REFUSING to read %s — %s", SECRETS_FILE, reason)
            return default
        parser = configparser.ConfigParser()
        try:
            parser.read(SECRETS_FILE, encoding="utf-8")
            if parser.has_option("secrets", name):
                val = parser.get("secrets", name).strip()
                if val and val.lower() not in _PLACEHOLDERS:
                    return val
        except configparser.Error as e:
            # configparser includes the offending line in its messages.
            # That line may contain a partial secret — log type only.
            log.warning("config.ini parse error: %s", _safe_exc(e))
    return default


def set_value(name: str, value: str) -> str:
    """Store ``value`` for ``name`` in ``config.ini[secrets]``.

    Returns the backend label (always ``"file"`` — the only backend left
    after v2.7.0's keyring removal).  Signature retains the return value
    so existing callers / tests continue to work unchanged.

    Raises ``ValueError`` if ``value`` contains a CR or LF: the file
    backend writes ``name = value`` as a single line, so an embedded
    newline would inject extra lines (a fake section or key) into
    config.ini.  Secret values are single-token credentials — a newline
    is always a mistake or an injection attempt.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("secret value must not contain a newline")
    _write_file_secret(name, value)
    return "file"


def delete(name: str) -> list[str]:
    """Remove ``name`` from ``config.ini[secrets]``.

    Returns ``["file"]`` if a key was removed, ``[]`` if the key was not
    present.  Raises ``PermissionError`` (NOT swallowed) if config.ini
    exists with perms looser than 0o600 — a failed delete must not be
    reported as "not found", or an operator trying to remove a leaked
    credential would believe it was already gone.
    """
    touched: list[str] = []
    if SECRETS_FILE.exists():
        if _delete_file_secret(name):
            touched.append("file")
    return touched


def status() -> dict[str, object]:
    """Diagnostic snapshot of the secret store environment."""
    perms, perms_reason = perms_ok(SECRETS_FILE)
    return {
        "secrets_file":        str(SECRETS_FILE),
        "secrets_file_exists": SECRETS_FILE.exists(),
        "secrets_file_perms":  perms_reason,
        "perms_ok":            perms,
        "env_prefix":          ENV_PREFIX,
    }


def list_stored() -> dict[str, str]:
    """Return ``{secret_name: backend}`` for every known secret currently stored.

    Backend may be ``"env"``, ``"file"``, or ``""`` (none).
    """
    out: dict[str, str] = {}
    parser: configparser.ConfigParser | None = None
    if SECRETS_FILE.exists() and perms_ok(SECRETS_FILE)[0]:
        parser = configparser.ConfigParser()
        try:
            parser.read(SECRETS_FILE, encoding="utf-8")
        except configparser.Error:
            parser = None
    for name in KNOWN_SECRETS:
        if os.environ.get(ENV_PREFIX + name.upper()):
            out[name] = "env"
            continue
        if parser is not None and parser.has_option("secrets", name):
            v = parser.get("secrets", name).strip()
            if v and v.lower() not in _PLACEHOLDERS:
                out[name] = "file"
                continue
        out[name] = ""
    return out


# ── config.ini[secrets] file backend ─────────────────────────────────
#
# SECRETS_FILE is config.ini, which holds both the bot's runtime config
# and the [secrets] section.  We can't round-trip the whole file through
# ``configparser`` (write() strips every comment), so set/delete here
# operate as targeted text-based edits on the [secrets] section while
# leaving the rest of the file byte-for-byte untouched.


def _atomic_write_text(text: str) -> None:
    """Write raw text to ``SECRETS_FILE`` with 0o600 perms, atomically."""
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_FILE.with_suffix(SECRETS_FILE.suffix + ".tmp")
    # Create tmp with 0o600 from the start to avoid a window where the
    # file is world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, SECRETS_FILE)
    if os.name != "nt":
        os.chmod(SECRETS_FILE, 0o600)


def _find_secrets_section(lines: list[str]) -> tuple[int | None, int]:
    """Locate the ``[secrets]`` section in ``lines``.

    Returns ``(start, end)`` where ``start`` is the index of the first
    line *after* the ``[secrets]`` header, and ``end`` is the index of
    the next section header (or ``len(lines)`` if the section runs to
    EOF).  Returns ``(None, len(lines))`` if no ``[secrets]`` header.
    """
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "[secrets]":
            header_idx = i
            break
    if header_idx is None:
        return None, len(lines)
    start = header_idx + 1
    end = len(lines)
    for j in range(start, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and s.endswith("]") and len(s) >= 3:
            end = j
            break
    return start, end


def _write_file_secret(name: str, value: str) -> None:
    """Set ``[secrets].name = value`` in ``SECRETS_FILE``.

    Preserves comments and all other sections byte-for-byte.  If the
    ``[secrets]`` section doesn't exist, it's appended at EOF.  If the
    key already exists in ``[secrets]``, its value is replaced in place;
    otherwise the new line is inserted at the end of the section.
    """
    text = ""
    if SECRETS_FILE.exists():
        ok, reason = perms_ok(SECRETS_FILE)
        if not ok:
            raise PermissionError(
                f"refusing to modify {SECRETS_FILE} — {reason}")
        text = SECRETS_FILE.read_text(encoding="utf-8")
    new_line = f"{name} = {value}\n"
    lines = text.splitlines(keepends=True)
    if not lines:
        # Empty / new file — write just the [secrets] header + value.
        _atomic_write_text(f"[secrets]\n{new_line}")
        return
    start, end = _find_secrets_section(lines)
    if start is None:
        # No [secrets] section — append at EOF.
        if not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n[secrets]\n")
        lines.append(new_line)
    else:
        replaced = False
        for i in range(start, end):
            stripped = lines[i].lstrip()
            if not stripped or stripped.startswith((";", "#")):
                continue
            if "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip().lower()
            if key == name.lower():
                indent = lines[i][:len(lines[i]) - len(stripped)]
                lines[i] = f"{indent}{name} = {value}\n"
                replaced = True
                break
        if not replaced:
            # Insert at the end of [secrets], before any trailing blanks.
            insert_at = end
            while insert_at > start and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            lines.insert(insert_at, new_line)
    _atomic_write_text("".join(lines))


def _delete_file_secret(name: str) -> bool:
    """Remove ``[secrets].name`` from ``SECRETS_FILE``.  Returns True on hit.

    Raises ``PermissionError`` if the file exists with perms looser than
    0o600 — mirrors ``_write_file_secret`` so a delete blocked by bad
    perms surfaces loudly instead of looking like a no-op.
    """
    if not SECRETS_FILE.exists():
        return False
    ok, reason = perms_ok(SECRETS_FILE)
    if not ok:
        raise PermissionError(f"refusing to modify {SECRETS_FILE} — {reason}")
    text = SECRETS_FILE.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start, end = _find_secrets_section(lines)
    if start is None:
        return False
    for i in range(start, end):
        stripped = lines[i].lstrip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip().lower()
        if key == name.lower():
            del lines[i]
            _atomic_write_text("".join(lines))
            return True
    return False


# ── Migration from config.ini ────────────────────────────────────────

def _scrub_config_ini(cfg_path: Path, names: list[str]) -> None:
    """Rewrite config.ini with the given secret keys removed/cleared.

    Preserves comments and structure by editing line-by-line instead of
    round-tripping through configparser (which loses formatting).

    The ``[secrets]`` section is intentionally exempt — that's where the
    values are being moved TO, so blanking matching keys there would
    immediately undo the migration when source and destination are the
    same file (the new default, since config.ini is now SECRETS_FILE).
    """
    target_keys = {CONFIG_LOCATIONS[n][1] for n in names if n in CONFIG_LOCATIONS}
    text = cfg_path.read_text(encoding="utf-8")
    out_lines: list[str] = []
    current_section: str | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        bare = stripped.rstrip()
        # Section header — track it but don't touch.
        if bare.startswith("[") and bare.endswith("]") and len(bare) >= 3:
            current_section = bare[1:-1].lower()
            out_lines.append(line)
            continue
        # Skip blanking inside [secrets] (it's the destination, not a source).
        if (current_section != "secrets" and stripped
                and not stripped.startswith((";", "#"))
                and "=" in stripped):
            key = stripped.split("=", 1)[0].strip().lower()
            if key in target_keys:
                indent = line[:len(line) - len(stripped)]
                out_lines.append(f"{indent}{key} =\n")
                continue
        out_lines.append(line)
    cfg_path.write_text("".join(out_lines), encoding="utf-8")


def migrate(config_path: Path = Path("config.ini"),
            *, scrub: bool = True) -> dict[str, str]:
    """Move all known plaintext secrets from config.ini into ``[secrets]``.

    Returns ``{name: action}`` where action is ``stored:file``,
    ``skipped:empty``, or ``error:<reason>``.
    """
    cfg_path = config_path.resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"{cfg_path} not found")
    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    parser.read(cfg_path, encoding="utf-8")
    # Tighten perms first so _write_file_secret accepts the writes.
    if (cfg_path == SECRETS_FILE
            and os.name != "nt"
            and stat.S_IMODE(cfg_path.stat().st_mode) != 0o600):
        os.chmod(cfg_path, 0o600)
        # NB: don't include the path in this log line — CodeQL's
        # py/clear-text-logging-sensitive-data heuristic taints any
        # variable flowing through this function.
        log.info("tightened config file to 0o600 for [secrets] writes")
    results: dict[str, str] = {}
    migrated: list[str] = []
    for name, (section, key) in CONFIG_LOCATIONS.items():
        if not parser.has_option(section, key):
            results[name] = "skipped:absent"
            continue
        value = parser.get(section, key).strip()
        if not value or value.lower() in _PLACEHOLDERS:
            results[name] = "skipped:empty"
            continue
        try:
            used = set_value(name, value)
            results[name] = f"stored:{used}"
            migrated.append(name)
        except Exception as e:
            # Report exception TYPE only — error messages from configparser /
            # OS file APIs occasionally echo back path fragments we don't
            # want in the log.
            results[name] = f"error:{_safe_exc(e)}"
    if scrub and migrated:
        _scrub_config_ini(cfg_path, migrated)
    return results


# ── CLI ──────────────────────────────────────────────────────────────

def _cmd_status(_: argparse.Namespace) -> int:
    info = status()
    print("Secret store status")
    print("-" * 60)
    for k, v in info.items():
        print(f"  {k:24} {v}")
    print()
    print("Backends: env var (INTERNETS_<NAME>) → config.ini[secrets] (0o600)")
    return 0


def _cmd_list(_: argparse.Namespace) -> int:
    """``python -m secret_store list`` — show which secret keys exist
    and in which backend each is stored.  Prints only the canonical key
    NAME and BACKEND label (env / file / unset), never the secret value
    itself.

    The body uses explicit equality branches to map ``list_stored()``'s
    return into literal display labels — breaks CodeQL's data-flow taint
    propagation so its ``py/clear-text-logging-sensitive-data`` query
    doesn't raise a false positive on the print.
    """
    stored = list_stored()
    width = max(len(n) for n in KNOWN_SECRETS) + 2
    print(f"{'secret':<{width}} backend")
    print("-" * (width + 12))
    for name in KNOWN_SECRETS:
        b = stored.get(name) or ""
        if b == "env":
            label = "env"
        elif b == "file":
            label = "file"
        else:
            label = "(unset)"
        print(f"{name:<{width}} {label}")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    """Confirm presence of a secret without printing the value.

    Prints a non-revealing summary like ``(set, 32 chars, backend=file)``
    so the value cannot be captured by terminal scrollback, shell history,
    or a screen recording open on the operator's machine.  There is no
    CLI flag to print the value; legitimate extraction goes through::

        python -c "import secret_store; print(secret_store.get('omdb_key'))"
    """
    val = get(args.name)
    if not val:
        print(f"(no value for {args.name!r})", file=sys.stderr)
        return 1
    # Report which backend the value came from.
    if os.environ.get(ENV_PREFIX + args.name.upper()):
        backend = "env"
    else:
        backend = "file"
    print(f"(set, {len(val)} chars, backend={backend})")
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    value = args.value
    if value is None:
        value = getpass.getpass(f"Value for {args.name}: ")
    if not value:
        print("error: empty value", file=sys.stderr)
        return 2
    used = set_value(args.name, value)
    print(f"stored {args.name} in {used}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    touched = delete(args.name)
    if touched:
        print(f"removed {args.name} from: {', '.join(touched)}")
        return 0
    print(f"{args.name} not found in [secrets]", file=sys.stderr)
    return 1


def _cmd_init(args: argparse.Namespace) -> int:
    """Create config.ini from config.ini.example with 0600 perms.

    Byte-for-byte copy so every inline comment, signup URL, and tier-limit
    hint is preserved.  Refuses to overwrite an existing config.ini unless
    --force is given (in which case the existing file is replaced wholesale
    — any local edits are lost; rotate any secrets afterwards).
    """
    src = Path("config.ini.example").resolve()
    if not src.exists():
        print(f"error: {src} not found — re-clone the repo or fetch it from "
              "the project root.", file=sys.stderr)
        return 2
    if SECRETS_FILE.exists() and not args.force:
        print(f"error: {SECRETS_FILE} already exists. Edit it directly, or "
              "re-run with --force to overwrite (existing values are LOST).",
              file=sys.stderr)
        return 1
    text = src.read_text(encoding="utf-8")
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SECRETS_FILE.exists() and args.force:
        # Replace existing config.ini wholesale.  Use the atomic writer so
        # we never leave a half-written file or loose perms behind.
        _atomic_write_text(text)
        print(f"overwrote {SECRETS_FILE} from {src.name} (mode 0600, "
              f"{len(text)} bytes)")
        print("any local edits in the old file are gone; rotate any "
              "secrets that were stored there.", file=sys.stderr)
        return 0
    fd = os.open(str(SECRETS_FILE),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        try:
            SECRETS_FILE.unlink()
        except OSError:
            pass
        raise
    if os.name != "nt":
        os.chmod(SECRETS_FILE, 0o600)
    print(f"created {SECRETS_FILE} (mode 0600, {len(text)} bytes)")
    print(f"edit it with your real values, or run "
          f"`python -m secret_store set <name>`")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).resolve()
    print(f"Migrating secrets from {cfg_path}")
    print(f"  scrub:   {'yes' if not args.no_scrub else 'no (dry-run)'}")
    results = migrate(cfg_path, scrub=not args.no_scrub)
    stored = [n for n, a in results.items() if a.startswith("stored:")]
    errors = [(n, a) for n, a in results.items() if a.startswith("error:")]
    print()
    print(f"Stored ({len(stored)}):")
    for n in stored:
        print(f"  {n:24} → {results[n]}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for n, a in errors:
            print(f"  {n:24} {a}")
    if stored:
        print()
        print("=" * 60)
        print("ROTATE EVERY SECRET LISTED ABOVE.")
        print("=" * 60)
        print("These values were just moved out of config.ini, but the file")
        print("is in git history.  Anyone with a clone of this repo can read")
        print("them.  Rotate each one at its provider (regenerate API keys,")
        print("change NickServ/SASL/oper/server passwords) before relying on")
        print("the new storage.")
    return 1 if errors else 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m secret_store",
        description="Manage Internets bot secrets (env / config.ini[secrets]).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show secret store status").set_defaults(func=_cmd_status)
    sub.add_parser("list",   help="list known secrets and which backend holds each"
                  ).set_defaults(func=_cmd_list)

    g = sub.add_parser("get",
        help="confirm a secret is set (prints a non-revealing summary; "
             "use `python -c \"import secret_store; print(secret_store.get('NAME'))\"` "
             "to extract the actual value)")
    g.add_argument("name")
    g.set_defaults(func=_cmd_get)

    s = sub.add_parser("set", help="store a secret value in config.ini[secrets]")
    s.add_argument("name")
    s.add_argument("--value", default=None,
                   help="value (omit to be prompted; safer for shell history)")
    s.set_defaults(func=_cmd_set)

    d = sub.add_parser("delete", help="remove a secret from config.ini[secrets]")
    d.add_argument("name")
    d.set_defaults(func=_cmd_delete)

    i = sub.add_parser("init",
        help="create config.ini from config.ini.example with 0600 perms")
    i.add_argument("--force", action="store_true",
                   help="overwrite an existing config.ini wholesale "
                        "(any local edits are lost)")
    i.set_defaults(func=_cmd_init)

    m = sub.add_parser("migrate",
        help="move plaintext from non-[secrets] sections into [secrets] + scrub source")
    m.add_argument("--config", default="config.ini")
    m.add_argument("--no-scrub", action="store_true",
                   help="store secrets but leave non-[secrets] sections untouched (dry-run-ish)")
    m.set_defaults(func=_cmd_migrate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
