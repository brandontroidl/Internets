class BotModule:
    """
    Base class for all bot modules.

    Subclasses define COMMANDS as a dict mapping command words to method names,
    implement those methods with the signature (self, nick, reply_to, arg),
    and override help_lines() to describe them.

    on_load / on_unload are optional hooks called by the module loader.
    """

    COMMANDS: dict = {}

    def __init__(self, bot):
        self.bot = bot

    def help_lines(self, prefix: str) -> list:
        return []

    def on_load(self):
        pass

    def on_unload(self):
        pass
