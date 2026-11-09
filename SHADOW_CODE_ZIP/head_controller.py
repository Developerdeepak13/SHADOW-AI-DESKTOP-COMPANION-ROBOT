"""Reliable Raspberry Pi -> ESP32 SHADOW head controller over HTTP."""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# Keep this aligned with the IP printed by the ESP32 Serial Monitor.
ESP32_IP = "10.206.118.41"
ESP32_TIMEOUT = 2.0
# Prevent duplicate movements when Gemini emits repeated/partial transcripts.
COMMAND_COOLDOWN_SECONDS = 0.65
_last_command = None
_last_command_at = 0.0

LEFT_PHRASES = (
    "see left", "look left", "turn left", "move left", "go left",
    "head left", "face left", "look to the left", "turn to the left",
    "look your left", "look on the left", "look over left", "look towards left",
    "turn your head left", "turn your head to the left", "move your head left",
    "look at the left", "look left side", "left dekho", "baaye dekho",
    "baye dekho", "baayi taraf dekho", "baaye taraf dekho", "left mein dekho",
    "बाएं देखो", "बाएँ देखो", "बाईं तरफ देखो", "बायीं तरफ देखो", "बाईं ओर देखो",
)
RIGHT_PHRASES = (
    "see right", "look right", "turn right", "move right", "go right",
    "head right", "face right", "look to the right", "turn to the right",
    "look your right", "look on the right", "look over right", "look towards right",
    "turn your head right", "turn your head to the right", "move your head right",
    "look at the right", "look right side", "right dekho", "daaye dekho",
    "daye dekho", "daayi taraf dekho", "daaye taraf dekho", "right mein dekho",
    "दाएं देखो", "दाएँ देखो", "दाईं तरफ देखो", "दायीं तरफ देखो", "दाईं ओर देखो",
)
CENTER_PHRASES = (
    "see front", "look front", "look straight", "look ahead", "face front",
    "face forward", "turn front", "turn to the front", "look at me", "look at us",
    "look center", "look to the center", "look in the center", "turn to center",
    "go center", "go to center", "move center", "head center", "center the head",
    "center yourself", "come to center", "come center", "back to center",
    "return to center", "look forward", "turn your head forward", "seedha dekho",
    "samne dekho", "saamne dekho", "beech mein dekho", "center mein aao",
    "सामने देखो", "सीधा देखो", "बीच में आओ", "सेंटर में आओ", "मेरी तरफ देखो",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).casefold().strip())


def _has_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    # English phrases use word boundaries to avoid accidental substring matches;
    # Hindi/Devanagari phrases are checked directly.
    for phrase in phrases:
        p = _normalize(phrase)
        if re.search(r"[a-z0-9]", p):
            if re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text):
                return True
        elif p in text:
            return True
    return False


def detect_head_direction(text: str) -> str | None:
    """Return left/right/center only for a likely intentional head command."""
    t = _normalize(text)
    if not t:
        return None

    # Center phrases first to avoid ambiguous phrases such as "look straight".
    if _has_phrase(t, CENTER_PHRASES):
        return "center"
    if _has_phrase(t, LEFT_PHRASES):
        return "left"
    if _has_phrase(t, RIGHT_PHRASES):
        return "right"

    # Flexible English patterns: "please turn your head to the left".
    if re.search(r"\b(?:please\s+)?(?:look|turn|move|point|face)\s+(?:your\s+)?head\s+(?:to\s+)?(?:the\s+)?left\b", t):
        return "left"
    if re.search(r"\b(?:please\s+)?(?:look|turn|move|point|face)\s+(?:your\s+)?head\s+(?:to\s+)?(?:the\s+)?right\b", t):
        return "right"
    if re.search(r"\b(?:please\s+)?(?:look|turn|move|face)\s+(?:yourself\s+)?(?:to\s+)?(?:the\s+)?left\b", t):
        return "left"
    if re.search(r"\b(?:please\s+)?(?:look|turn|move|face)\s+(?:yourself\s+)?(?:to\s+)?(?:the\s+)?right\b", t):
        return "right"
    if re.search(r"\b(?:please\s+)?(?:look|turn|move|face)\s+(?:your\s+)?head\s+(?:back\s+)?(?:to\s+)?(?:the\s+)?(?:center|middle|front|forward|straight)\b", t):
        return "center"
    return None


def _send(endpoint: str) -> bool:
    # Validate endpoint locally; never allow arbitrary URL input.
    if endpoint not in ("/left", "/right", "/center"):
        return False
    url = f"http://{ESP32_IP}{endpoint}"
    try:
        with urllib.request.urlopen(url, timeout=ESP32_TIMEOUT) as response:
            status = getattr(response, "status", 200)
            response.read()
            if not 200 <= status < 300:
                print(f"⚠️ ESP32 returned HTTP {status} for {endpoint}")
                return False
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"⚠️ ESP32 head command failed: {url} ({exc})")
        return False


def move_head(direction: str) -> bool:
    endpoint = {"left": "/left", "right": "/right", "center": "/center"}.get(direction)
    if endpoint is None:
        print(f"⚠️ Unknown head direction: {direction!r}")
        return False
    if _send(endpoint):
        print(f"🦾 ESP32 HEAD → {direction.upper()}")
        return True
    return False


def move_head_from_voice(text: str) -> str | None:
    """Detect and send a voice command. Returns direction only on HTTP success."""
    global _last_command, _last_command_at
    direction = detect_head_direction(text)
    if direction is None:
        return None

    now = time.monotonic()
    if direction == _last_command and now - _last_command_at < COMMAND_COOLDOWN_SECONDS:
        print(f"ℹ️ Ignored duplicate {direction.upper()} head command")
        return direction

    if move_head(direction):
        _last_command = direction
        _last_command_at = time.monotonic()
        return direction
    return None


def set_esp32_ip(ip: str) -> None:
    """Optional helper for changing the target IP at runtime."""
    global ESP32_IP
    candidate = ip.strip()
    # Restrict to a plain IPv4 address (no scheme/path/port).
    parts = candidate.split(".")
    if len(parts) != 4 or any(not part.isdigit() or not 0 <= int(part) <= 255 for part in parts):
        raise ValueError("ESP32_IP must be a plain IPv4 address, e.g. 192.168.1.50")
    ESP32_IP = candidate
