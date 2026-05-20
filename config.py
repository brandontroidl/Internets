"""Configuration loading and CLI argument parsing.

Reads ``config.ini`` at import time and exposes all parsed constants
used by the bot core, modules, and logging.

Outbound credentials (NickServ/SASL/server/oper passwords, API keys) are
pulled from ``secret_store`` first and fall back to the matching field
in ``config.ini`` only if the secret store has no value.  Run
``python -m secret_store migrate`` once to move plaintext out of
``config.ini`` into the OS keyring (or gitignored ``secrets.ini``).
"""

from __future__ import annotations

import argparse
import configparser
from pathlib import Path

import secret_store

__version__ = "2.6.0"


def _secret_or_cfg(secret_name: str, section: str, key: str, default: str = "") -> str:
    """Return secret_store value if set, else ``cfg[section][key]``, else default."""
    val = secret_store.get(secret_name)
    if val:
        return val
    if cfg.has_option(section, key):
        return cfg.get(section, key, fallback=default).strip()
    return default


# ── Config file ──────────────────────────────────────────────────────

cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
CONFIG_PATH = str(Path("config.ini").resolve())
# Load committed template first, then overlay personal overrides if
# present.  config.local.ini is gitignored; secrets stay in secret_store.
_LOCAL_CONFIG = Path("config.local.ini").resolve()


def reload_config() -> list[str]:
    """Re-read BOTH config.ini and config.local.ini into the live cfg.

    configparser's ``read()`` only overrides keys that exist in the
    file being re-read.  Re-reading config.ini alone (which carries
    empty placeholders for password_hash, default_location, etc.)
    silently clobbers values that were only set in config.local.ini.
    Every reload path — startup, SIGHUP, cmd_rehash, get_hash —
    must go through here so the overlay stays intact.

    Returns the list of files actually read (for caller logging).
    """
    files = cfg.read(CONFIG_PATH)
    if _LOCAL_CONFIG.exists():
        files += cfg.read(str(_LOCAL_CONFIG))
    return files


read_files = reload_config()

# ── IRC settings ─────────────────────────────────────────────────────

SERVER       = cfg["irc"]["server"]
PORT         = int(cfg["irc"]["port"])
NICKNAME     = cfg["irc"]["nickname"]
REALNAME     = cfg["irc"]["realname"]
# Credentials: secret_store wins, config.ini is the legacy fallback.
NS_PW        = _secret_or_cfg("nickserv_password", "irc", "nickserv_password")
SERVER_PW    = _secret_or_cfg("server_password",   "irc", "server_password")
OPER_N       = cfg["irc"].get("oper_name",     "").strip()
OPER_PW      = _secret_or_cfg("oper_password",     "irc", "oper_password")
USER_MODES   = cfg["irc"].get("user_modes",     "").strip()
OPER_MODES   = cfg["irc"].get("oper_modes",     "").strip()
OPER_SNOMASK = cfg["irc"].get("oper_snomask",   "").strip()

# ── Bot settings ─────────────────────────────────────────────────────

CMD_PREFIX  = cfg["bot"]["command_prefix"]
API_CD      = int(cfg["bot"]["api_cooldown"])
FLOOD_CD    = int(cfg["bot"].get("flood_cooldown", "3"))
MODULES_DIR = Path(cfg["bot"].get("modules_dir", "modules"))
AUTO_LOAD   = [m.strip() for m in cfg["bot"].get("autoload", "").split(",") if m.strip()]

# All optional — the bot works fine if the server supports none of these.
DESIRED_CAPS: set[str] = {
    "multi-prefix", "away-notify", "account-notify", "chghost",
    "extended-join", "server-time", "message-tags", "sasl",
}

# ── CLI ──────────────────────────────────────────────────────────────

_cli = argparse.ArgumentParser(
    description="Internets — async modular IRC bot",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""\
debug examples:
  %(prog)s --debug                   global debug (all subsystems)
  %(prog)s --debug weather store     debug only weather + store
  %(prog)s --loglevel WARNING        suppress INFO from console + main log
  %(prog)s --debug-file debug.log    capture all DEBUG to separate file
  %(prog)s --no-console              disable stdin command loop (for daemons)

interactive console (type 'help' at the > prompt while running):
  debug, loglevel, status, shutdown""")
_cli.add_argument("--version", action="version", version=f"Internets {__version__}")
_cli.add_argument("--debug", nargs="*", metavar="SUBSYSTEM", default=None,
                   help="enable debug output.  No args = global debug.  "
                        "With args = per-subsystem (e.g. --debug weather store)")
_cli.add_argument("--loglevel", metavar="LEVEL", default=None,
                   help="base log level: DEBUG, INFO, WARNING, ERROR")
_cli.add_argument("--debug-file", metavar="PATH", default=None,
                   help="write all DEBUG output to this file (overrides config)")
_cli.add_argument("--no-console", action="store_true", default=False,
                   help="disable interactive stdin console (for daemonized use)")
cli_args = _cli.parse_args()

# ── Logging config ───────────────────────────────────────────────────

LOG_LEVEL   = (cli_args.loglevel or cfg["logging"]["level"]).upper()
LOG_FILE    = cfg["logging"]["log_file"]
LOG_MAX     = int(cfg["logging"].get("max_bytes",    "5242880"))  # 5 MB default
LOG_BACKUPS = int(cfg["logging"].get("backup_count", "3"))
LOG_DEBUG   = cli_args.debug_file or cfg["logging"].get("debug_file", "").strip()
LOG_FMT     = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
