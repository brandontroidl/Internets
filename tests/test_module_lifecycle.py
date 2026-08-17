"""Phase 1 of the IRPG design: async lifecycle primitives for modules.

Three gaps block any module that owns a background task:

1. `on_unload()` is synchronous and `unload_module()` calls it synchronously, so
   a module can call `task.cancel()` but can never await the cancellation. After
   `.reload`, the replacement task can start while the old one is still winding
   down, which for a game clock means two clocks.
2. `unload_module()` removes registry entries but does not drain command tasks
   already dispatched, so a handler can run on and mutate state after the
   module's final flush.
3. A module cannot observe that the bot lost its IRC connection, so anything
   tracking presence keeps accruing against players who are no longer there.

These tests pin the primitives that close all three. They are core-level and
game-agnostic: any module with a periodic task needs them.

See docs/superpowers/specs/2026-08-16-irpg-module-design.md, phase 1.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest

_SAVED_ARGV = sys.argv
sys.argv = ["internets"]
import internets  # noqa: E402
from modules.base import BotModule  # noqa: E402

sys.argv = _SAVED_ARGV


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture
def bot(loop):
    b = internets.IRCBot.__new__(internets.IRCBot)
    b._mod_lock = threading.Lock()
    b._modules = {}
    b._commands = {}
    b._loop = loop
    b._module_tasks = {}
    return b


class _Ticker(BotModule):
    """A module that owns a periodic task, like the game clock will."""

    COMMANDS: dict[str, str] = {}

    def __init__(self):
        self.ticks = 0
        self.stopped = False

    async def _tick_forever(self):
        try:
            while True:
                self.ticks += 1
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            self.stopped = True
            raise


class TestModuleTaskRegistry:
    def test_task_is_registered_against_its_module(self, bot, loop):
        m = _Ticker()

        async def go():
            bot.create_module_task("ticker", m._tick_forever())
            await asyncio.sleep(0.01)
            assert len(bot._module_tasks.get("ticker", ())) == 1
            await bot.drain_module_tasks("ticker")

        loop.run_until_complete(go())

    def test_drain_cancels_and_awaits(self, bot, loop):
        """The whole point: after drain returns, the task is really finished."""
        m = _Ticker()

        async def go():
            bot.create_module_task("ticker", m._tick_forever())
            await asyncio.sleep(0.01)
            drained = await bot.drain_module_tasks("ticker")
            assert drained == 1
            assert m.stopped is True
            assert not bot._module_tasks.get("ticker")

        loop.run_until_complete(go())

    def test_drain_leaves_other_modules_running(self, bot, loop):
        a, b_ = _Ticker(), _Ticker()

        async def go():
            bot.create_module_task("a", a._tick_forever())
            bot.create_module_task("b", b_._tick_forever())
            await asyncio.sleep(0.01)
            await bot.drain_module_tasks("a")
            assert a.stopped and not b_.stopped
            await bot.drain_module_tasks("b")

        loop.run_until_complete(go())

    def test_finished_task_is_forgotten(self, bot, loop):
        """A completed task must not accumulate in the registry."""

        async def once():
            return 1

        async def go():
            bot.create_module_task("m", once())
            await asyncio.sleep(0.01)
            assert not bot._module_tasks.get("m")

        loop.run_until_complete(go())

    def test_drain_of_unknown_module_is_not_an_error(self, bot, loop):
        assert loop.run_until_complete(bot.drain_module_tasks("nope")) == 0

    def test_a_task_that_raises_does_not_break_drain(self, bot, loop):
        async def boom():
            raise RuntimeError("bad tick")

        async def go():
            bot.create_module_task("m", boom())
            await asyncio.sleep(0.01)
            assert await bot.drain_module_tasks("m") == 0

        loop.run_until_complete(go())


class TestReloadLeavesOneTask:
    def test_no_two_clocks_after_a_drain_and_restart(self, bot, loop):
        """The reload hazard the spec names: two clocks running at once."""
        first, second = _Ticker(), _Ticker()

        async def go():
            bot.create_module_task("irpg", first._tick_forever())
            await asyncio.sleep(0.01)
            await bot.drain_module_tasks("irpg")
            bot.create_module_task("irpg", second._tick_forever())
            await asyncio.sleep(0.01)
            assert len(bot._module_tasks["irpg"]) == 1
            assert first.stopped is True
            await bot.drain_module_tasks("irpg")

        loop.run_until_complete(go())


class _Watcher(BotModule):
    COMMANDS: dict[str, str] = {}

    def __init__(self):
        self.events: list[str] = []

    def on_connect(self) -> None:
        self.events.append("connect")

    def on_disconnect(self) -> None:
        self.events.append("disconnect")


class TestConnectionNotifications:
    def test_base_declares_both_hooks(self):
        assert hasattr(BotModule, "on_connect")
        assert hasattr(BotModule, "on_disconnect")

    def test_default_hooks_are_harmless(self):
        m = BotModule.__new__(BotModule)
        m.on_connect()
        m.on_disconnect()

    def test_bot_fans_out_to_modules(self, bot):
        w = _Watcher()
        bot._modules = {"w": w}
        bot._notify_modules("on_connect")
        bot._notify_modules("on_disconnect")
        assert w.events == ["connect", "disconnect"]

    def test_one_failing_module_does_not_stop_the_others(self, bot):
        class _Bad(BotModule):
            COMMANDS: dict[str, str] = {}

            def on_disconnect(self):
                raise RuntimeError("module is broken")

        good = _Watcher()
        bot._modules = {"bad": _Bad.__new__(_Bad), "good": good}
        bot._notify_modules("on_disconnect")
        assert good.events == ["disconnect"]


class TestSpawnHelper:
    def test_module_can_spawn_against_its_own_name(self, bot, loop):
        """A module should not have to know the registry to own a task."""
        m = _Ticker()
        m.bot = bot
        m._module_name = "ticker"

        async def go():
            m.spawn(m._tick_forever())
            await asyncio.sleep(0.01)
            assert len(bot._module_tasks["ticker"]) == 1
            await bot.drain_module_tasks("ticker")

        loop.run_until_complete(go())


class TestCommandTasksAreDrainable:
    """A dispatched handler outlives its module unless it is drained.

    `unload_module()` removes the registry entries, but the handler is already
    a scheduled task holding a bound method, so it keeps running and can mutate
    state after the module's final flush. For a game that means a command
    committing a change to a database nobody will write again.
    """

    def test_a_running_command_task_is_registered_to_its_module(self, bot, loop):
        started = asyncio.Event()
        finished = []

        async def slow_handler(nick, reply_to, arg):
            started.set()
            await asyncio.sleep(5)
            finished.append(True)

        async def go():
            bot._loop = asyncio.get_running_loop()
            bot._active_cmd_tasks = 0
            bot._stats_cmd_count = 0
            bot._tasks = []
            bot._commands = {"play": ("irpg", "cmd_play")}
            task = bot._loop.create_task(
                bot._run_cmd(slow_handler, "alice", "alice", None, "play"),
                name="cmd-play")
            bot._module_tasks.setdefault("irpg", set()).add(task)
            task.add_done_callback(lambda t: bot._forget_module_task("irpg", t))
            await started.wait()
            drained = await bot.drain_module_tasks("irpg")
            assert drained == 1
            assert not finished, "handler kept running after its module was drained"

        loop.run_until_complete(go())

    def test_dispatch_registers_module_command_tasks(self):
        """The dispatcher must do that registration, not just the test."""
        import inspect
        src = inspect.getsource(internets.IRCBot._dispatch)
        assert "_module_tasks" in src, (
            "_dispatch does not register command tasks against their module, "
            "so unload cannot drain them")
