"""
Async priority send queue with token-bucket rate limiting.

priority=0  protocol traffic (PONG, CAP, NICK, QUIT) — bypass bucket
priority=1  normal output (PRIVMSG, NOTICE, JOIN) — subject to bucket

Burst: 5 tokens.  Refill: 1 per 1.5s (~40 msg/min sustained).

Thread-safe: enqueue() can be called from any thread (module handlers
run in asyncio.to_thread).
"""
from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger("internets.sender")


class Sender:
    CAPACITY: int = 5
    REFILL: float = 1.5

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop   = loop
        self._q:      asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue()
        self._seq     = 0
        self._seq_lk  = threading.Lock()       # protects _seq from concurrent enqueue
        self._writer:  asyncio.StreamWriter | None = None
        self._task:    asyncio.Task[None] | None   = None

    def start(self, writer: asyncio.StreamWriter) -> None:
        """Begin draining the queue to *writer*.  Call from the event loop."""
        self._writer = writer
        self._q      = asyncio.PriorityQueue()
        with self._seq_lk:
            self._seq = 0
        self._task = asyncio.create_task(self._drain(), name="sender")

    async def stop(self) -> None:
        """Cancel the drain task.  Call from the event loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def enqueue(self, msg: str, priority: int = 1) -> None:
        """Thread-safe enqueue.  Safe to call from any thread."""
        with self._seq_lk:
            self._seq += 1
            seq = self._seq
        item = (priority, seq, msg)
        self._loop.call_soon_threadsafe(self._q.put_nowait, item)

    # Prefixes of outgoing IRC commands whose arguments contain secrets.
    _REDACT_OUT: tuple[str, ...] = (
        "PASS ", "OPER ", "PRIVMSG NickServ :IDENTIFY ", "AUTHENTICATE ",
    )

    # Maximum IRC line length including \r\n (RFC 2812 §2.3).
    _MAX_IRC_LINE = 512

    def _write_line(self, msg: str) -> None:
        """Sanitize, log, and buffer a single IRC line.  NOT async — just buffers."""
        # Strip embedded CR/LF/NUL to prevent protocol injection.
        msg = msg.replace("\r", "").replace("\n", "").replace("\x00", "")
        # BUG-026: Enforce 512-byte IRC line limit (including \r\n).
        encoded = msg.encode("utf-8", errors="replace")
        if len(encoded) > self._MAX_IRC_LINE - 2:  # reserve 2 for \r\n
            encoded = encoded[:self._MAX_IRC_LINE - 2]
            # Avoid splitting a multi-byte UTF-8 char.
            while encoded and (encoded[-1] & 0xC0) == 0x80:
                encoded = encoded[:-1]
            msg = encoded.decode("utf-8", errors="replace")
        # Redact credentials from logs.
        log_msg = msg
        for prefix in self._REDACT_OUT:
            if msg.upper().startswith(prefix.upper()):
                log_msg = prefix + "[REDACTED]"
                break
        log.debug(f">> {log_msg}")
        try:
            if self._writer and not self._writer.is_closing():
                self._writer.write((msg + "\r\n").encode("utf-8", errors="replace"))
        except Exception as e:
            log.warning(f"Send error: {e}")

    async def _drain(self) -> None:
        """Consume the queue, apply token-bucket rate limiting, write + drain."""
        tokens = float(self.CAPACITY)
        last   = self._loop.time()

        while True:
            try:
                pri, _, msg = await asyncio.wait_for(self._q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                # Replenish tokens even when idle.
                now    = self._loop.time()
                tokens = min(self.CAPACITY, tokens + (now - last) / self.REFILL)
                last   = now
                continue

            now    = self._loop.time()
            tokens = min(self.CAPACITY, tokens + (now - last) / self.REFILL)
            last   = now

            if pri > 0:
                # Normal traffic — wait for a token.
                while tokens < 1.0:
                    await asyncio.sleep(0.05)
                    now    = self._loop.time()
                    tokens = min(self.CAPACITY, tokens + (now - last) / self.REFILL)
                    last   = now
                tokens -= 1.0

            self._write_line(msg)

            # Flush the write buffer to the OS.
            try:
                if self._writer and not self._writer.is_closing():
                    await self._writer.drain()
            except Exception as e:
                log.warning(f"Drain error: {e}")
