"""
Async priority send queue with token-bucket rate limiting.

priority=0  protocol traffic (PONG, CAP, NICK, QUIT) - bypass bucket
priority=1  normal output (PRIVMSG, NOTICE, JOIN) - subject to bucket

Burst: 5 tokens.  Refill: 1 per 1.5s (~40 msg/min sustained).

Thread-safe: enqueue() can be called from any thread (module handlers
run in asyncio.to_thread).
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Callable

# ── Credential redaction (single source of truth, both directions) ───────
#
# One list of verbs that mean "a credential follows".  redact_secrets() is
# applied to every OUTBOUND line before it is logged (below) AND to inbound
# lines before internets.py logs them, so a credential cannot be redacted in
# one direction and leak in the other.  The verb, not its context, is the
# signal, which is why a `.raw identify <pw>` (no service target) is caught
# just as `PRIVMSG NickServ :IDENTIFY <pw>` is.
#
# Redaction is LOG-ONLY: the bytes on the wire are never altered.  Matching is
# word-boundaried, so a verb embedded in another word ("password", "compass")
# is not a false positive; a bare standalone verb in relayed .say text is
# over-redacted in the debug log, which is the safe side for a credential.
_SECRET_VERBS: tuple[str, ...] = (
    "AUTHENTICATE", "IDENTIFY", "REGISTER", "IDENT", "OPER", "PASS", "AUTH",
)
# Longest-first so IDENTIFY wins over IDENT and AUTHENTICATE over AUTH at the
# same position.  The verb (and its trailing space) is kept; everything after
# is masked.  `\S.*` requires real content after the verb, so a bare verb with
# no argument is left alone.
_RE_SECRET = re.compile(
    r"(?i)(\b(?:" + "|".join(sorted(_SECRET_VERBS, key=len, reverse=True))
    + r")\b\s+)(\S.*)")


def redact_secrets(text: str) -> str:
    """Mask the argument after the first credential verb, keeping the verb.

    Direction-agnostic on a bare command/text: `NS IDENTIFY pw` and
    `raw identify pw` both become `... [REDACTED]`.  Callers that pass a full
    inbound line with a `nick!user@host` prefix must scope this to the trailing
    text first (see internets._redact_inbound), or a host like `ident@h` would
    match the IDENT verb.
    """
    return _RE_SECRET.sub(lambda m: m.group(1) + "[REDACTED]", text, count=1)

log = logging.getLogger("internets.sender")


def _bump_dropped() -> None:
    """Best-effort: count an outbound message drop in the Prometheus metric.

    Sender has no bot reference, so it touches the global registry directly;
    a failure (metrics disabled / import issue) must never affect sending.
    """
    try:
        from metrics import registry as _mreg  # noqa: PLC0415
        _mreg.dropped_messages_total.inc()
    except Exception:  # noqa: BLE001
        pass  # nosec B110: best-effort cleanup


class Sender:
    """Async priority send queue with token-bucket rate limiting.

    Priority 0 bypasses the bucket (protocol traffic).  Priority 1 is
    subject to rate limiting (~40 msg/min sustained, 5 burst).
    ``enqueue()`` is thread-safe - modules call it from worker threads.
    """
    CAPACITY: int = 5
    REFILL: float = 1.5
    MAX_QUEUE: int = 200  # BUG-056: Bound queue to prevent OOM during disconnects

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 on_drop: Callable[[], None] | None = None) -> None:
        self._loop   = loop
        self._q:      asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue(maxsize=self.MAX_QUEUE)
        self._seq     = 0
        self._seq_lk  = threading.Lock()       # protects _seq from concurrent enqueue
        self._writer:  asyncio.StreamWriter | None = None
        self._task:    asyncio.Task[None] | None   = None
        # Optional callback the bot passes so a drop bumps its real
        # dropped-message counter (surfaced in the shutdown summary), not just
        # the Prometheus metric.  Runs on the event-loop thread inside _drop.
        self._on_drop = on_drop

    def start(self, writer: asyncio.StreamWriter) -> None:
        """Begin draining the queue to *writer*.  Call from the event loop."""
        self._writer = writer
        self._q      = asyncio.PriorityQueue(maxsize=self.MAX_QUEUE)
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

    def _drop(self) -> None:
        """Record one outbound drop: bump the Prometheus metric AND, if the bot
        wired one, its in-process counter (the honest shutdown-summary source).

        Runs in the event-loop thread (called from _safe_put).  The bot
        callback must never raise into the send path, so it's guarded.
        """
        _bump_dropped()
        if self._on_drop is not None:
            try:
                self._on_drop()
            except Exception:  # noqa: BLE001
                pass  # nosec B110: a counter bump must never break sending

    def _safe_put(self, item: tuple[int, int, str]) -> None:
        """Put an item on the queue, dropping if full.  Runs in event loop thread.

        Priority-0 traffic (PONG/CAP/NICK/QUIT) MUST NOT be dropped on
        overflow: losing a PONG causes a server ping-timeout disconnect,
        which produces a reconnect storm worse than the original overflow.
        On a full queue we evict a low-priority message from the *tail*
        before inserting the priority-0 item, so the bot stays connected
        even when chatty modules saturate the queue.
        """
        priority = item[0]
        try:
            self._q.put_nowait(item)
        except asyncio.QueueFull:
            if priority == 0:
                # Evict the lowest-priority/highest-seq slot to guarantee
                # protocol traffic enqueues.  PriorityQueue exposes its
                # heap as ._queue - using it is acceptable here because
                # the alternative is dropping a PONG.
                try:
                    heap = self._q._queue  # type: ignore[attr-defined]
                    if heap:
                        # Find the worst (largest pri/seq) entry and evict it.
                        worst_idx = max(range(len(heap)), key=lambda i: heap[i])
                        evicted = heap.pop(worst_idx)
                        # Heap invariant has to be restored after a pop()
                        # from an arbitrary index.
                        import heapq
                        heapq.heapify(heap)
                        log.warning(
                            f"Send queue full - evicted pri={evicted[0]} "
                            f"to make room for priority-0 traffic"
                        )
                        self._drop()
                        self._q.put_nowait(item)
                        return
                except Exception as e:  # pragma: no cover - defensive
                    log.error(f"Failed to evict for priority-0: {e}")
                # If eviction failed, log loudly - never silently drop pri-0.
                log.error("Send queue full - UNABLE to enqueue priority-0 message")
            else:
                log.warning("Send queue full - dropping message")
                self._drop()

    def enqueue(self, msg: str, priority: int = 1) -> None:
        """Thread-safe enqueue.  Safe to call from any thread."""
        with self._seq_lk:
            self._seq += 1
            seq = self._seq
        item = (priority, seq, msg)
        self._loop.call_soon_threadsafe(self._safe_put, item)

    # Maximum IRC line length including \r\n (RFC 2812 §2.3).
    _MAX_IRC_LINE = 512

    def _write_line(self, msg: str) -> None:
        """Sanitize, log, and buffer a single IRC line.  NOT async - just buffers."""
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
        # Redact credentials from the log only - the wire gets the full msg.
        # This catches the bot's own IDENTIFY/PASS/OPER/AUTHENTICATE and a
        # credential injected via `.raw` alike (module-level redact_secrets).
        log.debug(f">> {redact_secrets(msg)}")
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
                # Normal traffic - wait for a token.
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
