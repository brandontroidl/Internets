"""
Base class for all Internets modules.

Every module must:
  1. Subclass BotModule
  2. Define COMMANDS dict mapping command words → method names
  3. Implement help_lines(prefix) → list[str]
  4. Call super().__init__(bot) in __init__

Example:
    class MyModule(BotModule):
        COMMANDS = {"hello": "cmd_hello", "hi": "cmd_hello"}

        def __init__(self, bot):
            super().__init__(bot)

        def cmd_hello(self, nick, reply_to, arg):
            self.bot.privmsg(reply_to, f"Hello, {nick}!")

        def help_lines(self, prefix):
            return [f"  {prefix}hello   Say hello"]
"""


class BotModule:
    # Subclasses define this: {"command_word": "method_name", ...}
    COMMANDS: dict = {}

    def __init__(self, bot):
        """
        bot: the IRCBot instance — gives access to bot.privmsg(), bot.send(), etc.
        """
        self.bot = bot

    def help_lines(self, prefix: str) -> list:
        """Return a list of help strings for this module's commands."""
        return []

    def on_load(self):
        """Called when the module is loaded. Override for setup."""
        pass

    def on_unload(self):
        """Called just before the module is unloaded. Override for cleanup."""
        pass
