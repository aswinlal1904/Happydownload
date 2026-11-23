"""Adaptive bandwidth observer, dynamic connection tuner, and optional rate limiter."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class SpeedPoint:
    timestamp: float
    bps: float
    connections: int


class TokenBucketLimiter:
    """Thread-safe byte rate limiter (0 = unlimited)."""

    def __init__(self, rate_bps: int = 0) -> None:
        self._lock = threading.Lock()
        self._rate_bps = max(0, int(rate_bps))
        self._tokens = float(self._rate_bps)
        self._last = time.monotonic()

    def set_rate(self, rate_bps: int) -> None:
        with self._lock:
            self._rate_bps = max(0, int(rate_bps))
            self._tokens = float(self._rate_bps)
            self._last = time.monotonic()

    def get_rate(self) -> int:
        with self._lock:
            return self._rate_bps

    def acquire(self, num_bytes: int, stop_event: Optional[threading.Event] = None) -> None:
        if num_bytes <= 0:
            return
        while True:
            if stop_event and stop_event.is_set():
                return
            with self._lock:
                rate = self._rate_bps
                if rate <= 0:
                    return
                now = time.monotonic()
                elapsed = max(0.0, now - self._last)
                self._last = now
                burst = max(float(rate), 256 * 1024.0)
                self._tokens = min(burst, self._tokens + elapsed * rate)
                if self._tokens >= num_bytes:
                    self._tokens -= num_bytes
                    return
                deficit = num_bytes - self._tokens
                wait_sec = min(0.5, deficit / float(rate))
            if stop_event:
                stop_event.wait(wait_sec)
            else:
                time.sleep(wait_sec)


class AdaptiveController:
    """Observes live throughput and tunes active connection count to maximize speed without loss."""

    def __init__(
        self,
        initial_connections: int,
        min_connections: int,
        max_connections: int,
        adaptive_enabled: bool = True,
        accept_ranges: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self.accept_ranges = accept_ranges
        self.adaptive_enabled = adaptive_enabled and accept_ranges
        self.min_connections = max(1, min_connections)
        self.max_connections = max(self.min_connections, max_connections)
        if not accept_ranges:
            self.target_connections = 1
        else:
            self.target_connections = max(self.min_connections, min(self.max_connections, initial_connections))

        self.current_bps: float = 0.0
        self.ewma_bps: float = 0.0
        self.peak_bps: float = 0.0

        self._bytes_total: int = 0
        self._samples: Deque[Tuple[float, int]] = deque(maxlen=60)
        self._history: Deque[SpeedPoint] = deque(maxlen=150)

        self._last_eval_time: float = time.monotonic()
        self._last_eval_bytes: int = 0
        self._prev_window_bps: float = 0.0
        self._last_action: str = "init"  # init | up | down | hold
        self._cooldown_until: float = 0.0
        self._plateau_count: int = 0

        self._throttle_events: int = 0
        self._error_events: int = 0

        if not accept_ranges:
            self.reason = "Single-stream mode (server does not support byte ranges)"
        elif not self.adaptive_enabled:
            self.reason = f"Fixed concurrency ({self.target_connections} connections)"
        else:
            self.reason = f"Probing baseline at {self.target_connections} connections"

    def update_limits(self, min_connections: int, max_connections: int, adaptive_enabled: bool) -> None:
        with self._lock:
            self.min_connections = max(1, min_connections)
            self.max_connections = max(self.min_connections, max_connections)
            self.adaptive_enabled = adaptive_enabled and self.accept_ranges
            if not self.accept_ranges:
                self.target_connections = 1
            else:
                self.target_connections = max(
                    self.min_connections, min(self.max_connections, self.target_connections)
                )

    def record_bytes(self, nbytes: int) -> None:
        if nbytes <= 0:
            return
        with self._lock:
            self._bytes_total += nbytes

    def report_throttle(self) -> None:
        with self._lock:
            self._throttle_events += 1

    def report_error(self) -> None:
        with self._lock:
            self._error_events += 1

    def tick(self, active_workers: int, remaining_segments_exist: bool = True) -> int:
        """Called periodically (~0.5s) to update speed telemetry and tune connection count."""
        now = time.monotonic()
        wall_now = time.time()
        with self._lock:
            self._samples.append((now, self._bytes_total))
            # 2-second rolling window for smooth current speed
            cutoff = now - 2.0
            oldest_t, oldest_b = self._samples[0]
            for t, b in self._samples:
                if t >= cutoff:
                    oldest_t, oldest_b = t, b
                    break
            dt = max(0.05, now - oldest_t)
            if len(self._samples) >= 2 and now > oldest_t:
                inst_bps = max(0.0, (self._bytes_total - oldest_b) / dt)
            else:
                inst_bps = 0.0

            self.current_bps = inst_bps
            if self.ewma_bps <= 0.0:
                self.ewma_bps = inst_bps
            else:
                self.ewma_bps = 0.30 * inst_bps + 0.70 * self.ewma_bps

            if inst_bps > self.peak_bps:
                self.peak_bps = inst_bps

            self._history.append(SpeedPoint(timestamp=wall_now, bps=inst_bps, connections=active_workers))

            if not self.adaptive_enabled or not self.accept_ranges:
                return self.target_connections

            # Handle immediate throttle reaction
            if self._throttle_events > 0:
                old = self.target_connections
                step = max(1, old // 3)
                self.target_connections = max(self.min_connections, old - step)
                self._throttle_events = 0
                self._error_events = 0
                self._last_action = "down"
                self._cooldown_until = now + 12.0
                self._last_eval_time = now
                self._last_eval_bytes = self._bytes_total
                self.reason = (
                    f"Server rate-limit detected — backed off {old} -> {self.target_connections} connections"
                )
                return self.target_connections

            eval_interval = 4.0
            if now - self._last_eval_time < eval_interval:
                return self.target_connections

            win_dt = max(0.1, now - self._last_eval_time)
            win_bps = max(0.0, (self._bytes_total - self._last_eval_bytes) / win_dt)
            errors = self._error_events
            self._error_events = 0
            self._last_eval_time = now
            self._last_eval_bytes = self._bytes_total

            if now < self._cooldown_until:
                self._prev_window_bps = win_bps
                return self.target_connections

            # Too many transient socket errors in this window -> reduce connection pressure
            if errors >= max(3, self.target_connections // 2):
                old = self.target_connections
                if old > self.min_connections:
                    self.target_connections = max(self.min_connections, old - 1)
                    self._last_action = "down"
                    self._cooldown_until = now + 10.0
                    self.reason = (
                        f"Network instability ({errors} drops) — reduced to {self.target_connections} connections"
                    )
                self._prev_window_bps = win_bps
                return self.target_connections

            if not remaining_segments_exist:
                self._prev_window_bps = win_bps
                return self.target_connections

            prev = self._prev_window_bps
            self._prev_window_bps = win_bps

            if prev <= 0.0:
                # Initial window completed; gently probe upward if below max
                if self.target_connections < self.max_connections and win_bps > 0:
                    old = self.target_connections
                    self.target_connections = min(self.max_connections, old + 1)
                    self._last_action = "up"
                    self.reason = (
                        f"Baseline {win_bps / (1024 * 1024):.2f} MB/s — probing {old} -> {self.target_connections} connections"
                    )
                return self.target_connections

            ratio = (win_bps - prev) / max(1.0, prev)

            if self._last_action == "up":
                if ratio >= 0.06:
                    # Scaling up helped! Continue probing upward
                    self._plateau_count = 0
                    if self.target_connections < self.max_connections:
                        old = self.target_connections
                        step = 2 if (ratio >= 0.25 and old < 8 and old + 2 <= self.max_connections) else 1
                        self.target_connections = min(self.max_connections, old + step)
                        self._last_action = "up"
                        self.reason = (
                            f"+{ratio * 100:.0f}% throughput gain — scaling {old} -> {self.target_connections} connections"
                        )
                    else:
                        self._last_action = "hold"
                        self.reason = f"Max {self.target_connections} connections active ({win_bps / (1024 * 1024):.2f} MB/s)"
                elif ratio < -0.10:
                    # Adding connections hurt throughput (slow-link TCP contention) -> step back!
                    old = self.target_connections
                    self.target_connections = max(self.min_connections, old - 1)
                    self._last_action = "hold"
                    self._plateau_count = 0
                    self._cooldown_until = now + 15.0
                    self.reason = (
                        f"Link saturated — settled at optimal {self.target_connections} connections"
                    )
                else:
                    # Marginal change -> plateau
                    self._plateau_count += 1
                    if self._plateau_count >= 2:
                        self._last_action = "hold"
                        self._cooldown_until = now + 20.0
                        self.reason = (
                            f"Observed max throughput (~{max(win_bps, self.peak_bps) / (1024 * 1024):.2f} MB/s) at {self.target_connections} connections"
                        )
                    elif self.target_connections < self.max_connections:
                        old = self.target_connections
                        self.target_connections = min(self.max_connections, old + 1)
                        self._last_action = "up"
                        self.reason = f"Testing {self.target_connections} connections for extra headroom"
            else:
                # Re-probe occasionally if bandwidth conditions improved
                if self.target_connections < self.max_connections and win_bps > 0:
                    old = self.target_connections
                    self.target_connections = min(self.max_connections, old + 1)
                    self._last_action = "up"
                    self._plateau_count = 0
                    self.reason = f"Re-probing {old} -> {self.target_connections} connections"

            return self.target_connections

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "target_connections": self.target_connections,
                "current_bps": self.current_bps,
                "ewma_bps": self.ewma_bps,
                "peak_bps": self.peak_bps,
                "reason": self.reason,
                "adaptive_enabled": self.adaptive_enabled,
                "history": [
                    {"t": p.timestamp, "bps": p.bps, "connections": p.connections}
                    for p in self._history
                ],
            }

