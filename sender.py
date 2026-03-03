import queue
import time
import threading
import logging

log = logging.getLogger("internets")


class Sender:
    # priority=0  protocol traffic (PONG, CAP, NICK, QUIT) — bypass bucket, sent immediately
    # priority=1  normal output (PRIVMSG, NOTICE, JOIN) — subject to token bucket
    # Burst: 5 tokens. Refill: 1 per 1.5s (~40 msg/min sustained). Keeps us under flood kills.
    CAPACITY = 5
    REFILL   = 1.5

    def __init__(self):
        self.sock  = None
        self._q    = queue.PriorityQueue()
        self._seq  = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self, sock):
        self.sock = sock
        self._stop.clear()
        self._q   = queue.PriorityQueue()
        self._seq = 0
        threading.Thread(target=self._loop, daemon=True, name="sender").start()

    def stop(self):
        self._stop.set()

    def enqueue(self, msg, priority=1):
        with self._lock:
            self._seq += 1
            self._q.put((priority, self._seq, msg))

    # Prefixes of outgoing IRC commands whose arguments contain secrets.
    _REDACT_OUT = ("PASS ", "OPER ", "PRIVMSG NickServ :IDENTIFY ")

    def _write(self, msg):
        # Strip embedded CR/LF/NUL to prevent protocol injection.
        msg = msg.replace("\r", "").replace("\n", "").replace("\x00", "")
        # Redact credentials from logs.
        log_msg = msg
        for prefix in self._REDACT_OUT:
            if msg.upper().startswith(prefix.upper()):
                log_msg = prefix + "[REDACTED]"
                break
        log.debug(f">> {log_msg}")
        try:
            self.sock.sendall((msg + "\r\n").encode("utf-8", errors="replace"))
        except Exception as e:
            log.warning(f"Send error: {e}")

    def _loop(self):
        tokens = float(self.CAPACITY)
        last   = time.monotonic()

        while not self._stop.is_set():
            try:
                pri, _, msg = self._q.get(timeout=0.1)
            except queue.Empty:
                now    = time.monotonic()
                tokens = min(self.CAPACITY, tokens + (now - last) / self.REFILL)
                last   = now
                continue

            now    = time.monotonic()
            tokens = min(self.CAPACITY, tokens + (now - last) / self.REFILL)
            last   = now

            if pri == 0:
                self._write(msg)
            else:
                while tokens < 1.0 and not self._stop.is_set():
                    time.sleep(0.05)
                    now    = time.monotonic()
                    tokens = min(self.CAPACITY, tokens + (now - last) / self.REFILL)
                    last   = now
                tokens -= 1.0
                self._write(msg)
