import time
import threading


class EchoGate:
    """
    Prevents Shadow's speaker output from being
    immediately fed back into Gemini through the mic.

    We intentionally use a short hangover after playback
    instead of permanently muting the microphone.

    The main speaking-state gate is controlled by AudioPlayer.
    """

    def __init__(self, hangover_seconds: float = 0.25):
        self.hangover_seconds = hangover_seconds

        self._last_playback = 0.0
        self._lock = threading.Lock()

    def mark_playback(self):
        with self._lock:
            self._last_playback = time.monotonic()

    def is_in_hangover(self) -> bool:
        with self._lock:
            elapsed = (
                time.monotonic() - self._last_playback
            )

        return elapsed < self.hangover_seconds

    def remaining(self) -> float:
        with self._lock:
            elapsed = (
                time.monotonic() - self._last_playback
            )

        return max(
            0.0,
            self.hangover_seconds - elapsed,
        )