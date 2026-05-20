"""Internets bot modules — pluggable command handlers.

Each module exposes ``setup(bot) → BotModule``.  See ``base.py`` for the
interface.  Modules are loaded/unloaded at runtime via ``.load``/``.unload``.
"""

__all__ = ["base"]
