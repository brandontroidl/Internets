"""Tests for sender.py - async priority send queue with token-bucket rate limiting.

The project has no pytest-asyncio installed, so async behaviour is exercised by
driving a real event loop with ``loop.run_until_complete`` rather than with
``async def test_`` functions (which would silently no-op here).  Only true
externals are faked: the StreamWriter.  The Sender, its PriorityQueue, the token
bucket and the drain loop all run as real code against real objects.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from sender import Sender, _bump_dropped


# ── Fakes (only the StreamWriter is external) ────────────────────────────

class FakeWriter:
    """Minimal asyncio.StreamWriter stand-in: records bytes written."""

    def __init__(self, closing=False, raise_on_write=False, raise_on_drain=False):
        self.buf: list[bytes] = []
        self._closing = closing
        self._raise_on_write = raise_on_write
        self._raise_on_drain = raise_on_drain
        self.drain_calls = 0

    def is_closing(self) -> bool:
        return self._closing

    def write(self, data: bytes) -> None:
        if self._raise_on_write:
            raise OSError("write boom")
        self.buf.append(data)

    async def drain(self) -> None:
        self.drain_calls += 1
        if self._raise_on_drain:
            raise OSError("drain boom")


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


def _flush(loop):
    """Run the loop just long enough to execute pending call_soon callbacks."""
    loop.run_until_complete(asyncio.sleep(0))


# ── _bump_dropped / _drop / on_drop callback ─────────────────────────────

class TestDropAccounting:
    def test_bump_dropped_never_raises(self):
        # Touches the real global metrics registry; must be side-effect-safe.
        _bump_dropped()

    def test_drop_invokes_callback(self, loop):
        calls = []
        s = Sender(loop, on_drop=lambda: calls.append(1))
        s._drop()
        assert calls == [1]

    def test_drop_without_callback_is_safe(self, loop):
        s = Sender(loop)
        s._drop()  # no on_drop wired - must not raise

    def test_drop_swallows_callback_exception(self, loop):
        def boom():
            raise RuntimeError("counter exploded")

        s = Sender(loop, on_drop=boom)
        s._drop()  # a throwing counter must never break the send path


# ── _safe_put: queue cap, priority-0 eviction, priority ordering ─────────

class TestSafePut:
    def test_normal_put_enqueues(self, loop):
        s = Sender(loop)
        s._safe_put((1, 1, "hello"))
        assert s._q.qsize() == 1

    def test_priority_ordering(self, loop):
        s = Sender(loop)
        # Insert out of priority order; lower priority number must come out first
        # even with a higher sequence number.
        s._safe_put((1, 2, "normal2"))
        s._safe_put((0, 5, "proto"))   # protocol traffic, dequeued first
        s._safe_put((1, 1, "normal1"))
        out = [s._q.get_nowait()[2] for _ in range(3)]
        assert out == ["proto", "normal1", "normal2"]

    def test_queue_caps_at_max_queue(self, loop):
        s = Sender(loop)
        for i in range(Sender.MAX_QUEUE):
            s._safe_put((1, i, f"m{i}"))
        assert s._q.qsize() == Sender.MAX_QUEUE

    def test_full_queue_drops_low_priority(self, loop):
        calls = []
        s = Sender(loop, on_drop=lambda: calls.append(1))
        for i in range(Sender.MAX_QUEUE):
            s._safe_put((1, i, f"m{i}"))
        s._safe_put((1, 9999, "overflow"))  # queue full -> dropped
        assert s._q.qsize() == Sender.MAX_QUEUE
        assert calls == [1]
        heap_msgs = [t[2] for t in s._q._queue]
        assert "overflow" not in heap_msgs

    def test_priority0_evicts_worst_to_make_room(self, loop):
        calls = []
        s = Sender(loop, on_drop=lambda: calls.append(1))
        # Fill to capacity with 199 normal + 1 deliberately-worst (highest pri).
        for i in range(Sender.MAX_QUEUE - 1):
            s._safe_put((1, i, f"m{i}"))
        s._safe_put((5, 500, "WORST"))  # largest (pri, seq) tuple -> evicted
        assert s._q.qsize() == Sender.MAX_QUEUE

        s._safe_put((0, 1, "PONG"))  # protocol traffic must enqueue
        assert s._q.qsize() == Sender.MAX_QUEUE  # one evicted, one inserted
        heap_msgs = [t[2] for t in s._q._queue]
        assert "PONG" in heap_msgs       # never drop priority-0
        assert "WORST" not in heap_msgs  # the worst slot was evicted
        assert calls == [1]

    def test_priority0_evict_logs_warning(self, loop, caplog):
        s = Sender(loop)
        for i in range(Sender.MAX_QUEUE):
            s._safe_put((1, i, f"m{i}"))
        with caplog.at_level(logging.WARNING, logger="internets.sender"):
            s._safe_put((0, 1, "PONG"))
        assert "evicted" in caplog.text


# ── enqueue (thread-safe public API) ─────────────────────────────────────

class TestEnqueue:
    def test_enqueue_schedules_and_assigns_increasing_seq(self, loop):
        s = Sender(loop)
        s.enqueue("a", 1)
        s.enqueue("b", 1)
        _flush(loop)  # let call_soon_threadsafe callbacks run
        assert s._q.qsize() == 2
        items = sorted(s._q.get_nowait() for _ in range(2))
        # seq monotonically increasing from 1
        assert items[0][1] == 1
        assert items[1][1] == 2

    def test_enqueue_default_priority_is_one(self, loop):
        s = Sender(loop)
        s.enqueue("msg")
        _flush(loop)
        pri, _seq, msg = s._q.get_nowait()
        assert pri == 1
        assert msg == "msg"

    def test_enqueue_priority_zero_passthrough(self, loop):
        s = Sender(loop)
        s.enqueue("PONG :x", 0)
        _flush(loop)
        pri, _seq, msg = s._q.get_nowait()
        assert pri == 0


# ── _write_line: sanitization, truncation, redaction, writer guards ──────

class TestWriteLine:
    def _sender(self, loop, **wkw):
        s = Sender(loop)
        w = FakeWriter(**wkw)
        s._writer = w
        return s, w

    def test_short_line_written_with_crlf(self, loop):
        s, w = self._sender(loop)
        s._write_line("PING x")
        assert w.buf == [b"PING x\r\n"]

    def test_strips_cr_lf_nul_injection(self, loop):
        s, w = self._sender(loop)
        s._write_line("PRIVMSG #c :ha\r\nQUIT\x00")
        data = w.buf[0]
        body = data[:-2]  # drop the single appended CRLF
        assert data.endswith(b"\r\n")
        assert b"\x00" not in body
        assert b"\r" not in body and b"\n" not in body
        # Injected newline/QUIT collapsed onto one line.
        assert body == b"PRIVMSG #c :haQUIT"

    def test_truncates_long_ascii_to_512(self, loop):
        s, w = self._sender(loop)
        s._write_line("a" * 600)
        data = w.buf[0]
        assert len(data) == 512                # hard RFC 2812 cap
        assert data == b"a" * 510 + b"\r\n"     # 510 payload + CRLF

    def test_truncates_multibyte_without_overrun(self, loop):
        s, w = self._sender(loop)
        s._write_line("€" * 200)  # 600 bytes of 3-byte chars
        data = w.buf[0]
        assert len(data) <= 512
        assert data.endswith(b"\r\n")
        assert len(data) - 2 <= Sender._MAX_IRC_LINE - 2
        # Result decodes without raising (errors='replace').
        data[:-2].decode("utf-8", errors="replace")

    def test_exact_boundary_not_truncated(self, loop):
        s, w = self._sender(loop)
        s._write_line("b" * 510)  # exactly the payload limit
        assert w.buf[0] == b"b" * 510 + b"\r\n"

    def test_no_writer_does_not_crash(self, loop):
        s = Sender(loop)  # _writer is None
        s._write_line("hi")  # must be a no-op, not an error

    def test_closing_writer_not_written(self, loop):
        s, w = self._sender(loop, closing=True)
        s._write_line("hi")
        assert w.buf == []

    def test_write_error_swallowed_and_logged(self, loop, caplog):
        s, w = self._sender(loop, raise_on_write=True)
        with caplog.at_level(logging.WARNING, logger="internets.sender"):
            s._write_line("hi")  # writer raises -> must not propagate
        assert "Send error" in caplog.text


# ── redaction is log-only: the wire still gets the full line ─────────────

class TestRedaction:
    SECRETS = [
        ("PASS supersecretpw", "PASS "),
        ("OPER adminname operpass123", "OPER "),
        ("PRIVMSG NickServ :IDENTIFY nspassword", "PRIVMSG NickServ :IDENTIFY "),
        ("NS IDENTIFY nsshortpw", "NS IDENTIFY "),
        ("CHANSERV IDENTIFY #chan chpw", "CHANSERV IDENTIFY "),
        ("AUTHENTICATE c2VjcmV0YmxvYg==", "AUTHENTICATE "),
        ("IDENT genericpw", "IDENT "),
    ]

    @pytest.mark.parametrize("line,prefix", SECRETS)
    def test_secret_redacted_in_log(self, loop, caplog, line, prefix):
        s = Sender(loop)
        w = FakeWriter()
        s._writer = w
        with caplog.at_level(logging.DEBUG, logger="internets.sender"):
            s._write_line(line)
        secret = line.split()[-1]
        assert secret not in caplog.text          # secret never logged
        assert "[REDACTED]" in caplog.text
        assert prefix in caplog.text               # canonical prefix shown
        # Redaction must NOT alter the bytes actually sent to the server.
        assert w.buf[0] == (line + "\r\n").encode("utf-8")

    def test_redaction_is_case_insensitive(self, loop, caplog):
        s = Sender(loop)
        w = FakeWriter()
        s._writer = w
        with caplog.at_level(logging.DEBUG, logger="internets.sender"):
            s._write_line("privmsg nickserv :identify lowerpw")
        assert "lowerpw" not in caplog.text
        assert "[REDACTED]" in caplog.text
        # Wire still gets the verbatim (lowercase) line.
        assert w.buf[0] == b"privmsg nickserv :identify lowerpw\r\n"

    def test_normal_line_not_redacted(self, loop, caplog):
        s = Sender(loop)
        w = FakeWriter()
        s._writer = w
        with caplog.at_level(logging.DEBUG, logger="internets.sender"):
            s._write_line("PRIVMSG #chan :hello world")
        assert "[REDACTED]" not in caplog.text
        assert "hello world" in caplog.text


# ── start / stop lifecycle ───────────────────────────────────────────────

class TestLifecycle:
    def test_start_creates_task_and_resets_state(self, loop):
        w = FakeWriter()
        s = Sender(loop)
        s._seq = 99  # dirty state from a prior connection

        async def run():
            s.start(w)
            assert s._task is not None
            assert s._writer is w
            assert s._seq == 0       # seq reset on (re)start
            await s.stop()
            assert s._task is None

        loop.run_until_complete(run())

    def test_stop_without_start_is_noop(self, loop):
        s = Sender(loop)
        loop.run_until_complete(s.stop())  # _task is None - must not raise


# ── _drain: integration of rate limiting + priority bypass ───────────────

class TestDrainRateLimiting:
    def test_priority1_burst_then_throttle(self, loop):
        """5-token burst lets 5 through immediately; the 6th waits for refill."""
        w = FakeWriter()
        s = Sender(loop)

        async def run():
            s.start(w)
            for i in range(6):
                s.enqueue(f"p1_{i}", 1)
            await asyncio.sleep(0.4)  # < REFILL (1.5s) so no extra token yet
            await s.stop()

        loop.run_until_complete(run())
        assert len(w.buf) == 5  # exactly the burst capacity; 6th throttled

    def test_priority0_bypasses_bucket_and_orders_first(self, loop):
        """Priority-0 traffic ignores the token bucket and is drained first."""
        w = FakeWriter()
        s = Sender(loop)

        async def run():
            s.start(w)
            for i in range(20):
                s.enqueue(f"p0_{i}", 0)   # protocol traffic, unthrottled
            for i in range(6):
                s.enqueue(f"p1_{i}", 1)   # normal traffic, bucket-limited
            await asyncio.sleep(0.4)
            await s.stop()

        loop.run_until_complete(run())
        written = [b.decode("utf-8").rstrip("\r\n") for b in w.buf]
        # 20 priority-0 (all bypass) + 5 priority-1 burst = 25.
        assert len(written) == 25
        assert all(m.startswith("p0_") for m in written[:20])
        assert all(m.startswith("p1_") for m in written[20:])

    def test_idle_refill_then_send(self, loop):
        """An idle period exercises the wait_for timeout / refill branch."""
        w = FakeWriter()
        s = Sender(loop)

        async def run():
            s.start(w)
            await asyncio.sleep(0.3)  # > 0.25 get() timeout -> idle refill branch
            s.enqueue("hello-after-idle", 0)
            await asyncio.sleep(0.05)
            await s.stop()

        loop.run_until_complete(run())
        assert any(b"hello-after-idle" in x for x in w.buf)

    def test_closing_writer_skips_flush(self, loop):
        """A closing writer drains the queue without writing or flushing."""
        w = FakeWriter(closing=True)
        s = Sender(loop)

        async def run():
            s.start(w)
            s.enqueue("hi", 0)
            await asyncio.sleep(0.05)
            await s.stop()

        loop.run_until_complete(run())
        assert w.buf == []          # nothing written to a closing writer
        assert w.drain_calls == 0   # flush skipped too

    def test_drain_error_is_swallowed(self, loop, caplog):
        w = FakeWriter(raise_on_drain=True)
        s = Sender(loop)

        async def run():
            s.start(w)
            s.enqueue("hi", 0)  # priority-0 avoids the token wait
            await asyncio.sleep(0.1)
            await s.stop()

        with caplog.at_level(logging.WARNING, logger="internets.sender"):
            loop.run_until_complete(run())
        assert w.buf  # the write itself succeeded
        assert "Drain error" in caplog.text
