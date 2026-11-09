import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")


if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing from .env")

GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"
GEMINI_MEMORY_MODEL = "gemini-3.1-flash-lite"

DB_PATH = BASE_DIR / "shadow.db"

INPUT_SAMPLE_RATE = 48000
INPUT_CHANNELS = 1
# Gemini accepts 16-bit PCM and will resample, but SHADOW downsamples
# the 48 kHz USB mic stream locally so the capture device stays stable.
GEMINI_INPUT_SAMPLE_RATE = 16000

OUTPUT_SAMPLE_RATE = 24000
OUTPUT_CHANNELS = 1

# 100 ms hardware capture block (48 kHz mono).
CHUNK_SIZE = 4800

# Camera / touch hardware settings.
CAMERA_CAPTURE_TIMEOUT_SECONDS = 8
TOUCH_SERIAL_PORT = os.getenv("TOUCH_SERIAL_PORT", "auto")
TOUCH_BAUDRATE = int(os.getenv("TOUCH_BAUDRATE", "115200"))
TOUCH_HAPPY_SECONDS = float(os.getenv("TOUCH_HAPPY_SECONDS", "2.5"))

PREFIX_PADDING_MS = 80
SILENCE_DURATION_MS = 350

GEMINI_VOICE = "puck"
SHADOW_NAME = "Shadow"

MAX_RECENT_CONVERSATION_TURNS = 20
MAX_QUIZ_HISTORY = 20

REALTIME_MENTOR_ENABLED = True
REALTIME_MOCK_INTERVIEW_ENABLED = True

WEATHER_CACHE_TTL_SECONDS = 6 * 60 * 60
WEATHER_RESULT_TTL_SECONDS = 30
TAVILY_MAX_RESULTS = 3
TAVILY_CONTENT_CHARS = 700
