import datetime
import os
import sqlite3
import time
import uuid
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from tavily import TavilyClient

from config import (
    TAVILY_API_KEY,
    MAX_QUIZ_HISTORY,
    WEATHER_CACHE_TTL_SECONDS,
    WEATHER_RESULT_TTL_SECONDS,
    TAVILY_MAX_RESULTS,
    TAVILY_CONTENT_CHARS,
    DB_PATH,
)
from database import (
    save_fact,
    create_reminder_record,
    cancel_reminder_record,
    create_study_session,
    get_study_session,
    update_study_session,
    get_study_summary,
)



# ============================================================
# TAVILY
# ============================================================

tavily_client = None

if TAVILY_API_KEY:
    tavily_client = TavilyClient(
        api_key=TAVILY_API_KEY
    )


# ============================================================
# REUSABLE HTTP SESSION
# ============================================================

http = requests.Session()


# ============================================================
# REMINDER CONFIG
# ============================================================

# SHADOW is currently being developed for India.
SHADOW_TIMEZONE = ZoneInfo("Asia/Kolkata")


def _get_reminder_db_connection():
    """Open a short-lived SQLite connection for reminder operations."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# WEATHER CACHE
# ============================================================

weather_location_cache = {}
weather_result_cache = {}


# ============================================================
# QUIZ STATE
# ============================================================

quiz_history = []
current_quiz = None


# ============================================================
# WEATHER
# ============================================================

def _city_key(city: str) -> str:
    return " ".join(
        city.strip().lower().split()
    )


def _get_coordinates(city: str):
    key = _city_key(city)

    cached = weather_location_cache.get(key)

    if cached:
        cached_at, latitude, longitude, display_name = cached

        if (
            time.monotonic() - cached_at
            < WEATHER_CACHE_TTL_SECONDS
        ):
            return {
                "latitude": latitude,
                "longitude": longitude,
                "display_name": display_name,
            }

    response = http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={
            "name": city,
            "count": 1,
            "language": "en",
            "format": "json",
        },
        timeout=5,
    )

    response.raise_for_status()

    data = response.json()
    results = data.get("results", [])

    if not results:
        return None

    location = results[0]

    latitude = location["latitude"]
    longitude = location["longitude"]

    display_name = location.get(
        "name",
        city,
    )

    weather_location_cache[key] = (
        time.monotonic(),
        latitude,
        longitude,
        display_name,
    )

    return {
        "latitude": latitude,
        "longitude": longitude,
        "display_name": display_name,
    }


def get_weather(city: str):
    """
    Optimized weather lookup.
    """

    city = city.strip()

    if not city:
        return {
            "success": False,
            "error": "City name is required.",
        }

    key = _city_key(city)

    cached_result = weather_result_cache.get(key)

    if cached_result:
        cached_at, result = cached_result

        if (
            time.monotonic() - cached_at
            < WEATHER_RESULT_TTL_SECONDS
        ):
            result = dict(result)
            result["cached"] = True
            return result

    try:
        location = _get_coordinates(city)

        if not location:
            return {
                "success": False,
                "error": f"Could not find city: {city}",
            }

        response = http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": (
                    "temperature_2m,"
                    "relative_humidity_2m,"
                    "apparent_temperature,"
                    "weather_code"
                ),
            },
            timeout=5,
        )

        response.raise_for_status()

        weather = response.json()
        current = weather["current"]

        result = {
            "success": True,
            "city": location["display_name"],
            "temperature_c": current["temperature_2m"],
            "feels_like_c": current["apparent_temperature"],
            "humidity": current["relative_humidity_2m"],
            "weather_code": current["weather_code"],
            "cached": False,
        }

        weather_result_cache[key] = (
            time.monotonic(),
            result,
        )

        return result

    except requests.Timeout:
        return {
            "success": False,
            "error": "Weather service timed out.",
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


# ============================================================
# TIME
# ============================================================

def get_time():
    now = datetime.datetime.now()

    return {
        "time": now.strftime("%I:%M %p"),
        "date": now.strftime("%A, %d %B %Y"),
    }


# ============================================================
# SAVE USER FACT
# ============================================================

def save_user_fact(
    conn,
    fact: str,
    category: str,
):
    save_fact(
        conn,
        fact,
        category,
    )

    return {
        "success": True,
        "message": "Fact saved.",
    }


# ============================================================
# WEB SEARCH
# ============================================================

def search_web(query: str):

    if not tavily_client:
        return {
            "success": False,
            "error": "Tavily API key is not configured.",
        }

    query = query.strip()

    if not query:
        return {
            "success": False,
            "error": "Search query is empty.",
        }

    try:

        result = tavily_client.search(
            query=query,
            search_depth="basic",
            max_results=TAVILY_MAX_RESULTS,
        )

        cleaned = []

        for item in result.get(
            "results",
            [],
        ):

            content = item.get(
                "content",
                "",
            )

            content = content[
                :TAVILY_CONTENT_CHARS
            ]

            cleaned.append(
                {
                    "title": item.get(
                        "title",
                        "",
                    ),
                    "content": content,
                    "url": item.get(
                        "url",
                        "",
                    ),
                }
            )

        return {
            "success": True,
            "results": cleaned,
        }

    except Exception as e:

        return {
            "success": False,
            "error": str(e),
        }


# ============================================================
# SMART REMINDER
# ============================================================

def _normalize_reminder_datetime(value: str):
    """Parse an ISO datetime and return it as IST."""

    value = value.strip()

    if not value:
        return None

    try:
        parsed = datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=SHADOW_TIMEZONE
        )
    else:
        parsed = parsed.astimezone(
            SHADOW_TIMEZONE
        )

    return parsed


def create_reminder(
    title: str,
    scheduled_at: str = "",
    delay_seconds: int = 0,
    priority: str = "normal",
    notification_channel: str = "shadow",
):
    """
    Create a reminder directly in SQLite.

    No n8n, no webhook, and no HTTP request.
    Relative reminders are calculated locally by Python.
    """

    title = str(title or "").strip()
    scheduled_at = str(scheduled_at or "").strip()

    try:
        delay_seconds = int(delay_seconds or 0)
    except (TypeError, ValueError):
        delay_seconds = 0

    priority = str(priority or "normal").strip().lower()
    notification_channel = str(
        notification_channel or "shadow"
    ).strip().lower()

    if not title:
        return {
            "success": False,
            "error": "Reminder title is required.",
        }

    if delay_seconds < 0:
        return {
            "success": False,
            "error": "delay_seconds cannot be negative.",
        }

    if delay_seconds > 0 and scheduled_at:
        return {
            "success": False,
            "error": (
                "Use either delay_seconds or scheduled_at, "
                "not both."
            ),
        }

    if delay_seconds == 0 and not scheduled_at:
        return {
            "success": False,
            "error": (
                "A reminder time is required. "
                "Provide delay_seconds for relative "
                "reminders or scheduled_at for an "
                "absolute reminder."
            ),
        }

    if priority not in {
        "normal",
        "important",
        "urgent",
    }:
        priority = "normal"

    if notification_channel != "shadow":
        notification_channel = "shadow"

    # --------------------------------------------------------
    # Calculate exact reminder time locally.
    # --------------------------------------------------------

    if delay_seconds > 0:
        scheduled = (
            datetime.datetime.now(
                SHADOW_TIMEZONE
            )
            + timedelta(
                seconds=delay_seconds
            )
        )
    else:
        scheduled = _normalize_reminder_datetime(
            scheduled_at
        )

        if scheduled is None:
            return {
                "success": False,
                "error": (
                    "Invalid scheduled_at. Use ISO 8601, "
                    "for example "
                    "2026-08-26T19:00:00+05:30."
                ),
            }

    now = datetime.datetime.now(
        SHADOW_TIMEZONE
    )

    if scheduled <= now:
        return {
            "success": False,
            "error": (
                "The reminder time is in the past. "
                "Please choose a future time."
            ),
        }

    # Store the timestamp without timezone because the
    # database reminder worker compares it against UTC.
    scheduled_utc = scheduled.astimezone(
        timezone.utc
    )

    scheduled_db = scheduled_utc.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    reminder_id = str(uuid.uuid4())

    conn = None

    try:
        conn = _get_reminder_db_connection()

        result = create_reminder_record(
            conn=conn,
            reminder_id=reminder_id,
            title=title,
            scheduled_at=scheduled_db,
            priority=priority,
            notification_channel=notification_channel,
        )

        # Return the user-friendly IST time too.
        result["timezone"] = "Asia/Kolkata"
        result["scheduled_at"] = scheduled.isoformat(
            timespec="seconds"
        )

        return result

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }

    finally:
        if conn is not None:
            conn.close()


# ============================================================
# CANCEL REMINDER
# ============================================================

def cancel_reminder(
    reminder_id: str,
):
    """
    Cancel an existing reminder directly in SQLite.

    Once status becomes 'cancelled', the Python reminder
    worker will never deliver it.
    """

    reminder_id = str(
        reminder_id or ""
    ).strip()

    if not reminder_id:
        return {
            "success": False,
            "cancelled": False,
            "error": "reminder_id is required.",
        }

    conn = None

    try:
        conn = _get_reminder_db_connection()

        result = cancel_reminder_record(
            conn=conn,
            reminder_id=reminder_id,
        )

        if result.get("success"):
            result["cancelled"] = True
        else:
            result["cancelled"] = False

        return result

    except Exception as e:
        return {
            "success": False,
            "cancelled": False,
            "error": str(e),
        }

    finally:
        if conn is not None:
            conn.close()


# ============================================================
# QUIZ START
# ============================================================

def start_quiz(
    question: str,
    option_a: str,
    option_b: str,
    option_c: str,
    option_d: str,
    correct_answer: str,
    explanation: str = "",
):

    global current_quiz

    correct = correct_answer.strip().upper()

    if correct not in {
        "A",
        "B",
        "C",
        "D",
    }:

        return {
            "success": False,
            "error": (
                "correct_answer must be A, B, C, or D."
            ),
        }

    question = question.strip()

    if not question:

        return {
            "success": False,
            "error": "Question cannot be empty.",
        }

    quiz = {
        "question": question,
        "options": {
            "A": option_a.strip(),
            "B": option_b.strip(),
            "C": option_c.strip(),
            "D": option_d.strip(),
        },
        "correct_answer": correct,
        "explanation": explanation.strip(),
    }

    current_quiz = quiz

    quiz_history.append(question)

    if len(quiz_history) > MAX_QUIZ_HISTORY:

        del quiz_history[
            :-MAX_QUIZ_HISTORY
        ]

    return {
        "success": True,
        "message": "Quiz question stored.",
        "question_number": len(quiz_history),
    }


# ============================================================
# QUIZ ANSWER
# ============================================================

def check_quiz_answer(
    user_answer: str,
):

    if not current_quiz:

        return {
            "success": False,
            "error": (
                "There is no active quiz question."
            ),
        }

    raw = user_answer.strip()

    if not raw:

        return {
            "success": False,
            "error": "No answer was provided.",
        }

    normalized = raw.lower().strip()

    answer_map = {

        "a": "A",
        "option a": "A",
        "answer a": "A",
        "choice a": "A",
        "first": "A",
        "first one": "A",

        "b": "B",
        "option b": "B",
        "answer b": "B",
        "choice b": "B",
        "second": "B",
        "second one": "B",

        "c": "C",
        "option c": "C",
        "answer c": "C",
        "choice c": "C",
        "third": "C",
        "third one": "C",

        "d": "D",
        "option d": "D",
        "answer d": "D",
        "choice d": "D",
        "fourth": "D",
        "fourth one": "D",
    }

    selected = answer_map.get(
        normalized
    )

    if not selected and normalized:

        first = normalized[0].upper()

        if first in {
            "A",
            "B",
            "C",
            "D",
        }:

            selected = first

    correct = (
        selected
        == current_quiz["correct_answer"]
    )

    return {
        "success": True,
        "result": {
            "correct": correct,
            "selected_answer": selected,
            "correct_answer": (
                current_quiz["correct_answer"]
            ),
            "explanation": (
                current_quiz["explanation"]
            ),
        },
    }


# ============================================================
# STUDY / POMODORO
# ============================================================

def start_study_session(topic: str, focus_minutes: int = 25, break_minutes: int = 5, cycles: int = 4):
    topic = str(topic or '').strip() or 'Study'
    try:
        focus_minutes = int(focus_minutes)
        break_minutes = int(break_minutes)
        cycles = int(cycles)
    except (TypeError, ValueError):
        return {'success': False, 'error': 'Study times and cycles must be numbers.'}
    if not 1 <= focus_minutes <= 180:
        return {'success': False, 'error': 'focus_minutes must be between 1 and 180.'}
    if not 1 <= break_minutes <= 60:
        return {'success': False, 'error': 'break_minutes must be between 1 and 60.'}
    if not 1 <= cycles <= 20:
        return {'success': False, 'error': 'cycles must be between 1 and 20.'}

    now = datetime.datetime.now(timezone.utc)
    end = now + timedelta(minutes=focus_minutes)
    session_id = str(uuid.uuid4())
    conn = None
    try:
        conn = _get_reminder_db_connection()
        row = create_study_session(
            conn, session_id, topic, focus_minutes, break_minutes, cycles,
            now.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')
        )
        return {
            'success': True, 'session_id': session_id, 'topic': topic,
            'phase': 'focus', 'current_cycle': 1, 'cycles': cycles,
            'focus_minutes': focus_minutes, 'break_minutes': break_minutes,
            'message': f'Started a {focus_minutes}-minute focus session for {topic}.',
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn is not None: conn.close()


def pause_study_session():
    conn = None
    try:
        conn = _get_reminder_db_connection()
        row = get_study_session(conn)
        if not row or row['status'] != 'active':
            return {'success': False, 'error': 'There is no active study session to pause.'}
        row_keys = row.keys()
        end = datetime.datetime.strptime(row['phase_end_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        now = datetime.datetime.now(timezone.utc)
        remaining = max(0, int((end - now).total_seconds()))
        phase_started = datetime.datetime.strptime(row['phase_started_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        elapsed = max(0, int((now - phase_started).total_seconds()))
        if row['phase'] == 'focus':
            total_focus = int(row['total_focus_seconds'] or 0) + elapsed
            update_study_session(conn, row['session_id'], status='paused', paused_remaining_seconds=remaining, total_focus_seconds=total_focus)
        else:
            total_break = int(row['total_break_seconds'] or 0) + elapsed
            update_study_session(conn, row['session_id'], status='paused', paused_remaining_seconds=remaining, total_break_seconds=total_break)
        return {'success': True, 'session_id': row['session_id'], 'phase': row['phase'], 'remaining_seconds': remaining}
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn is not None: conn.close()


def resume_study_session():
    conn = None
    try:
        conn = _get_reminder_db_connection()
        row = get_study_session(conn)
        if not row or row['status'] != 'paused':
            return {'success': False, 'error': 'There is no paused study session to resume.'}
        remaining = int(row['paused_remaining_seconds'] or 0)
        now = datetime.datetime.now(timezone.utc)
        end = now + timedelta(seconds=remaining)
        update_study_session(conn, row['session_id'], status='active', phase_started_at=now.strftime('%Y-%m-%d %H:%M:%S'), phase_end_at=end.strftime('%Y-%m-%d %H:%M:%S'), paused_remaining_seconds=None)
        return {'success': True, 'session_id': row['session_id'], 'phase': row['phase'], 'remaining_seconds': remaining}
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn is not None: conn.close()


def stop_study_session():
    conn = None
    try:
        conn = _get_reminder_db_connection()
        row = get_study_session(conn)
        if not row:
            return {'success': False, 'error': 'There is no active study session.'}
        if row['status'] == 'active' and row['phase_end_at'] and row['phase_started_at']:
            now = datetime.datetime.now(timezone.utc)
            started = datetime.datetime.strptime(row['phase_started_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            elapsed = max(0, int((now - started).total_seconds()))
            if row['phase'] == 'focus':
                update_study_session(conn, row['session_id'], status='stopped', total_focus_seconds=int(row['total_focus_seconds'] or 0) + elapsed)
            else:
                update_study_session(conn, row['session_id'], status='stopped', total_break_seconds=int(row['total_break_seconds'] or 0) + elapsed)
        else:
            update_study_session(conn, row['session_id'], status='stopped')
        return {'success': True, 'session_id': row['session_id'], 'message': 'Study session stopped.'}
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn is not None: conn.close()


def get_study_status():
    conn = None
    try:
        conn = _get_reminder_db_connection()
        row = get_study_session(conn)
        if not row:
            return {'success': True, 'active': False, 'message': 'No active study session.'}
        remaining = 0
        if row['status'] == 'active' and row['phase_end_at']:
            end = datetime.datetime.strptime(row['phase_end_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            remaining = max(0, int((end - datetime.datetime.now(timezone.utc)).total_seconds()))
        elif row['status'] == 'paused':
            remaining = int(row['paused_remaining_seconds'] or 0)
        return {'success': True, 'active': row['status'] in ('active','paused'), 'session_id': row['session_id'], 'topic': row['topic'], 'phase': row['phase'], 'status': row['status'], 'current_cycle': row['current_cycle'], 'cycles': row['cycles_target'], 'remaining_seconds': remaining, 'focus_minutes': row['focus_minutes'], 'break_minutes': row['break_minutes']}
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn is not None: conn.close()


def get_study_progress(days: int = 1):
    conn = None
    try:
        conn = _get_reminder_db_connection()
        return get_study_summary(conn, days=max(1, min(int(days), 30)))
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn is not None: conn.close()


# ============================================================
# GEMINI FUNCTION DECLARATIONS
# ============================================================

FUNCTION_DECLARATIONS = [

    {
        "name": "look_at_camera",
        "description": (
            "Capture one current camera frame when the user asks what is this, "
            "what am I looking at, identify this object, or asks Shadow to "
            "describe something in front of the camera. The camera frame is "
            "provided to Gemini for visual understanding. Do not use this tool "
            "for ordinary questions that do not require the camera."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "get_weather",
        "description": (
            "Get current weather for a city."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name.",
                }
            },
            "required": ["city"],
        },
    },

    {
        "name": "get_time",
        "description": (
            "Get the current local system time and date."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "save_user_fact",
        "description": (
            "Save a durable fact about the user "
            "to long-term memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {

                "fact": {
                    "type": "string",
                    "description": (
                        "A durable fact about the user."
                    ),
                },

                "category": {
                    "type": "string",
                    "description": (
                        "identity, preference, "
                        "topic_strength, topic_weakness, "
                        "goal, or other."
                    ),
                },
            },
            "required": [
                "fact",
                "category",
            ],
        },
    },

    {
        "name": "search_web",
        "description": (
            "Search the live internet for current "
            "events, recent information, or facts "
            "that may have changed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                }
            },
            "required": ["query"],
        },
    },

    {
        "name": "start_quiz",
        "description": (
            "Store a multiple-choice quiz question that "
            "Gemini has already generated. This is a FAST "
            "LOCAL tool. Do not use GPT-5 to generate the "
            "question. Gemini must provide the exact "
            "question, four options, correct answer, and "
            "short explanation."
        ),
        "parameters": {
            "type": "object",
            "properties": {

                "question": {
                    "type": "string",
                    "description": "The quiz question.",
                },

                "option_a": {
                    "type": "string",
                    "description": "Option A.",
                },

                "option_b": {
                    "type": "string",
                    "description": "Option B.",
                },

                "option_c": {
                    "type": "string",
                    "description": "Option C.",
                },

                "option_d": {
                    "type": "string",
                    "description": "Option D.",
                },

                "correct_answer": {
                    "type": "string",
                    "description": "A, B, C, or D.",
                },

                "explanation": {
                    "type": "string",
                    "description": (
                        "Short explanation of the answer."
                    ),
                },
            },
            "required": [
                "question",
                "option_a",
                "option_b",
                "option_c",
                "option_d",
                "correct_answer",
            ],
        },
    },

    {
        "name": "check_quiz_answer",
        "description": (
            "Check the user's answer to the most recent "
            "quiz question. This is a FAST LOCAL tool and "
            "does not call another LLM."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_answer": {
                    "type": "string",
                    "description": (
                        "The user's spoken answer."
                    ),
                }
            },
            "required": ["user_answer"],
        },
    },

    {
        "name": "start_study_session",
        "description": (
            "Start a local study Pomodoro session. Use for study/focus requests. "
            "Default is 25 minutes focus, 5 minutes break, 4 cycles. "
            "This is a FAST LOCAL tool and does not call GPT-5."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Subject or topic to study."},
                "focus_minutes": {"type": "integer", "description": "Focus duration in minutes. Default 25."},
                "break_minutes": {"type": "integer", "description": "Break duration in minutes. Default 5."},
                "cycles": {"type": "integer", "description": "Number of focus cycles. Default 4."},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "pause_study_session",
        "description": "Pause the current study timer. FAST LOCAL tool.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "resume_study_session",
        "description": "Resume the paused study timer. FAST LOCAL tool.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_study_session",
        "description": "Stop the current study session. FAST LOCAL tool.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_study_status",
        "description": "Get current study timer status. FAST LOCAL tool.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_study_progress",
        "description": "Get study time and completed session statistics. FAST LOCAL tool.",
        "parameters": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Number of recent days, 1 to 30. Default 1."}},
        },
    },

    {
        "name": "create_reminder",
        "description": (
            "Create a reminder locally in SQLite. For relative requests "
            "such as 'in 10 seconds', 'in 5 minutes', or 'in 2 hours', "
            "use delay_seconds. Python calculates the exact time. For absolute requests "
            "such as 'tomorrow at 7 PM', use scheduled_at in ISO 8601 "
            "with India timezone +05:30. Do not use UTC for an India-local "
            "reminder. Ask for clarification if the requested time is "
            "ambiguous."
        ),
        "parameters": {
            "type": "object",
            "properties": {

                "title": {
                    "type": "string",
                    "description": (
                        "What the user should be reminded about."
                    ),
                },

                "scheduled_at": {
                    "type": "string",
                    "description": (
                        "Absolute future date/time in ISO 8601 with +05:30, "
                        "for example 2026-08-26T19:00:00+05:30. "
                        "Leave empty for relative reminders."
                    ),
                },

                "delay_seconds": {
                    "type": "integer",
                    "description": (
                        "Delay from now in seconds. Use this for "
                        "'in N seconds/minutes/hours'. "
                        "Examples: 10 seconds = 10, "
                        "5 minutes = 300, 2 hours = 7200. "
                        "Use 0 for absolute reminders."
                    ),
                },

                "priority": {
                    "type": "string",
                    "enum": [
                        "normal",
                        "important",
                        "urgent",
                    ],
                    "description": (
                        "Reminder priority. Default is normal."
                    ),
                },

                "notification_channel": {
                    "type": "string",
                    "enum": ["shadow"],
                    "description": (
                        "Use shadow for robot voice notification."
                    ),
                },
            },
            "required": ["title"],
        },
    },

    {
        "name": "cancel_reminder",
        "description": (
            "Cancel an existing reminder immediately. "
            "Use this whenever the user asks to cancel, "
            "delete, stop, remove, or forget a reminder. "
            "Use the reminder_id returned when that reminder "
            "was created. If the user says 'cancel that reminder', "
            "use the most recently created reminder_id. "
            "If this tool returns success=True, the reminder "
            "has been cancelled successfully. Tell the user "
            "clearly that the reminder is cancelled. "
            "Do not say that the cancellation tool is unavailable "
            "or broken when success=True."
        ),
        "parameters": {
            "type": "object",
            "properties": {

                "reminder_id": {
                    "type": "string",
                    "description": (
                        "The unique ID of the reminder to cancel. "
                        "Use the ID returned when the reminder "
                        "was created."
                    ),
                },
            },
            "required": ["reminder_id"],
        },
    },
]


# ============================================================
# GEMINI TOOLS
# ============================================================

GEMINI_TOOLS = [
    {
        "function_declarations": FUNCTION_DECLARATIONS
    }
]
