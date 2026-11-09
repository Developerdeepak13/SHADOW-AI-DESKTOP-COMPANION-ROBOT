import json
import sqlite3
import traceback
import uuid
import datetime
import time
import asyncio
import re
import unicodedata
from pathlib import Path
from gui import expression_window, expression_management, available_expressions
import threading

#import threading

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEY,
    GEMINI_LIVE_MODEL,
    GEMINI_VOICE,
    DB_PATH,
    PREFIX_PADDING_MS,
    SILENCE_DURATION_MS,
    MAX_RECENT_CONVERSATION_TURNS,
)

from database import (
    init_db,
    log_turn,
    get_recent_conversation_text,
    get_due_reminders,
    mark_reminder_delivered,
)

from memory import (
    load_long_term_memory_summary,
    consolidate_session_memory,
)

from agent import build_instructions

from audio_io import (
    AudioPlayer,
    microphone,
    receive_audio,
)
from camera import capture_frame
from touch_sensor import ShadowTouchSensor
from head_controller import move_head_from_voice

from tools import (
    GEMINI_TOOLS,
    get_weather,
    get_time,
    save_user_fact,
    search_web,
    start_quiz,
    check_quiz_answer,
    create_reminder,
    cancel_reminder,
    start_study_session,
    pause_study_session,
    resume_study_session,
    stop_study_session,
    get_study_status,
    get_study_progress,
)


# ============================================================
# SHADOW SESSION
# ============================================================
def is_gemini_goaway_error(error: Exception) -> bool:
    """Return True when Gemini closed the session because of GoAway/session duration."""
    message = str(error).lower()

    return (
        "goaway" in message
        or "go away" in message
        or "session duration" in message
        or "failed to close the connection after receiving a goaway" in message
        or "1008" in message
    )

class Shadow:

    def __init__(self, conn, state=None):

        self.conn = conn
        
        self.state = state

        self.session_id = str(
            uuid.uuid4()
        )

        # ----------------------------------------------------
        # GLOBAL SESSION CONTROL
        # ----------------------------------------------------

        self.stop_event = asyncio.Event()

        # Set when Gemini retires the current Live session (GoAway/session duration).
        # This tells SHADOW to reconnect the Gemini session instead of shutting down.
        self.restart_event = asyncio.Event()

        # Set only when the active Gemini session should return to passive mode.
        # IMPORTANT: this is separate from stop_event, which is reserved for
        # shutting down the whole SHADOW application.
        self.passive_request_event = asyncio.Event()

        # ----------------------------------------------------
        # AUDIO STATE
        # ----------------------------------------------------

        self.speaking_event = asyncio.Event()
        self.tool_event = asyncio.Event()

        # ----------------------------------------------------
        # TRANSCRIPT BUFFER
        # ----------------------------------------------------

        self.user_transcript = []
        self.assistant_transcript = []

        # ----------------------------------------------------
        # TOOL LOCK
        # ----------------------------------------------------

        self.tool_lock = asyncio.Lock()

        # ----------------------------------------------------
        # SESSION STATE
        # ----------------------------------------------------

        self.session_alive = True

        # ----------------------------------------------------
        # LAST REMINDER STATE
        # ----------------------------------------------------
        # Used for commands such as: "cancel that reminder".
        self.last_reminder_id = None
        self.last_reminder_title = None

        # ----------------------------------------------------
        # REMINDER DELIVERY STATE
        # ----------------------------------------------------
        # True only while an already-scheduled SQLite reminder is
        # being announced through Gemini Live.
        self.reminder_delivery_active = False
        self.pending_reminder_text = None

        # Expression mode: keeps the mentor expression active during a mock interview.
        self.interview_mode = False

        # Manual expression selected by the user through voice.
        # This survives Gemini's temporary "speaking" animation and is restored
        # when the turn finishes.
        self.requested_expression = "neutral"
        self.manual_expression_active = False
        self.expression_command_active = False
        self.emotion_expression_active = False
        self.expression_reset_task = None

        # ----------------------------------------------------
        # STUDY COACH
        # ----------------------------------------------------
        # Pomodoro/focus timer is local and persistent in SQLite.
        # The worker below only announces phase changes through Gemini Live.
        self.study_delivery_active = False

        # ----------------------------------------------------
        # WAKE WORD / ACTIVE-PASSIVE STATE
        # ----------------------------------------------------
        self.passive_mode = True
        self.idle_timeout_triggered = False
        self.active_timeout_seconds = 30
        self.last_user_activity = time.monotonic()
        self.activation_reason = None
        self.memory_initialized = False
        self._reminder_triggered = set()
        self.live_session = None
        # GPIO-16 sensor is created only after Gemini Live connects.
        # Touch expression temporarily takes priority over normal face resets.
        self.touch_sensor = None
        self.touch_expression_active = False

        # ----------------------------------------------------
        # REMINDER SYSTEM
        # ----------------------------------------------------
        # Reminders are stored and scheduled locally in SQLite.
        # No n8n webhook, HTTP server, or external scheduler is used.

    # ========================================================
    # REMINDER WORKER
    # ========================================================

    async def reminder_worker(self, session):
        """Deliver due SQLite reminders through Gemini Live."""

        while not self.stop_event.is_set():
            try:
                due_reminders = await asyncio.to_thread(
                    self._get_due_reminders
                )

                for reminder in due_reminders:
                    if self.stop_event.is_set():
                        break

                    (
                        _db_id,
                        reminder_id,
                        title,
                        _scheduled_at,
                        _priority,
                        _notification_channel,
                        _status,
                    ) = reminder

                    title = str(
                        title or "You have a reminder."
                    ).strip() or "You have a reminder."

                    # If passive reminder watcher already claimed this reminder,
                    # the active-session delivery path will handle it.
                    if reminder_id in self._reminder_triggered:
                        continue

                    # Re-check status immediately before delivery.
                    # If the user cancelled it, it is skipped.
                    current_status = await asyncio.to_thread(
                        self._get_reminder_status,
                        reminder_id,
                    )

                    if current_status != "active":
                        continue

                    self.last_reminder_id = reminder_id
                    self.last_reminder_title = title

                    reminder_text = (
                        "INTERNAL REMINDER DELIVERY. "
                        "This reminder has ALREADY been scheduled. "
                        "Do not create, edit, cancel, snooze, or "
                        "reschedule any reminder. "
                        "Simply announce this reminder naturally "
                        "and briefly to the user. "
                        "Do not mention SQLite, APIs, tools, or "
                        "internal systems. "
                        f"Reminder: {title}"
                    )

                    print()
                    print("🔔 Reminder due from SQLite.")
                    print(f"   Title: {title}")
                    print(f"   ID: {reminder_id}")

                    self.reminder_delivery_active = True

                    # Expression: active reminder delivery.
                    expression_management("remainder")

                    try:
                        await session.send_realtime_input(
                            text=reminder_text
                        )

                        await asyncio.to_thread(
                            self._mark_reminder_delivered,
                            reminder_id,
                        )

                        print(
                            "🔔 Reminder delivered to Gemini: "
                            f"{title}"
                        )

                    except Exception:
                        self.reminder_delivery_active = False
                        raise

                # 250 ms polling keeps short reminders responsive.
                await asyncio.sleep(0.25)

            except asyncio.CancelledError:
                break

            except Exception as e:
                print(f"❌ Reminder worker error: {e}")
                await asyncio.sleep(0.5)

    def _get_due_reminders(self):
        conn = None

        try:
            conn = sqlite3.connect(DB_PATH)
            return get_due_reminders(conn)

        finally:
            if conn is not None:
                conn.close()

    def _get_reminder_status(self, reminder_id):
        conn = None

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT status
                FROM reminders
                WHERE reminder_id = ?
                LIMIT 1
                """,
                (reminder_id,),
            )

            row = cursor.fetchone()

            return row[0] if row else None

        finally:
            if conn is not None:
                conn.close()

    def _mark_reminder_delivered(self, reminder_id):
        conn = None

        try:
            conn = sqlite3.connect(DB_PATH)

            mark_reminder_delivered(
                conn,
                reminder_id,
            )

        finally:
            if conn is not None:
                conn.close()

    # ========================================================
    # STUDY TIMER WORKER
    # ========================================================

    async def study_worker(self, session):
        """Watch the SQLite study session and announce focus/break transitions."""
        while not self.stop_event.is_set():
            try:
                result = await asyncio.to_thread(get_study_status)
                if result.get('active') and result.get('status') == 'active' and result.get('remaining_seconds', 0) <= 0:
                    await self._advance_study_phase(session)
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ Study worker error: {e}")
                await asyncio.sleep(0.5)

    async def _advance_study_phase(self, session):
        from database import get_study_session, update_study_session
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            row = get_study_session(conn)
            if not row or row['status'] != 'active':
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            phase_started = datetime.datetime.strptime(row['phase_started_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
            phase_elapsed = max(0, int((now - phase_started).total_seconds()))
            if row['phase'] == 'focus':
                focus_elapsed = min(phase_elapsed, row['focus_minutes'] * 60)
                total_focus = int(row['total_focus_seconds'] or 0) + focus_elapsed
                if row['current_cycle'] >= row['cycles_target']:
                    update_study_session(conn, row['session_id'], status='completed', phase='focus', total_focus_seconds=total_focus, completed_at=now.strftime('%Y-%m-%d %H:%M:%S'))
                    text = f"Study session complete for {row['topic']}. You finished {row['cycles_target']} focus cycles. Nice work, bro!"
                else:
                    end = now + datetime.timedelta(minutes=row['break_minutes'])
                    update_study_session(conn, row['session_id'], phase='break', phase_started_at=now.strftime('%Y-%m-%d %H:%M:%S'), phase_end_at=end.strftime('%Y-%m-%d %H:%M:%S'), total_focus_seconds=total_focus)
                    text = f"Focus session complete. Take a {row['break_minutes']}-minute break, bro. Cycle {row['current_cycle']} done."
            else:
                next_cycle = row['current_cycle'] + 1
                end = now + datetime.timedelta(minutes=row['focus_minutes'])
                break_elapsed = min(phase_elapsed, row['break_minutes'] * 60)
                update_study_session(conn, row['session_id'], phase='focus', current_cycle=next_cycle, phase_started_at=now.strftime('%Y-%m-%d %H:%M:%S'), phase_end_at=end.strftime('%Y-%m-%d %H:%M:%S'), total_break_seconds=int(row['total_break_seconds'] or 0) + break_elapsed)
                text = f"Break's over, bro. Starting focus cycle {next_cycle} for {row['topic']} now."

            print(f"⏱️ Study coach: {text}")
            await session.send_realtime_input(text=("INTERNAL STUDY COACH NOTIFICATION. Announce this naturally and briefly. " + text))
        finally:
            if conn is not None:
                conn.close()

    # ========================================================
    # ACTIVE SESSION TIMEOUT
    # ========================================================

    async def active_timeout_worker(self):
        """Return SHADOW to passive wake-word mode after inactivity."""
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(1)
                idle_for = time.monotonic() - self.last_user_activity

                if idle_for >= self.active_timeout_seconds:
                    self.idle_timeout_triggered = True
                    print()
                    print(f"😴 No user speech for {self.active_timeout_seconds} seconds.")
                    print("🟢 Returning to passive wake-word listening...")

                    # IMPORTANT: persist the latest conversation BEFORE
                    # shutting down the active Gemini session. This makes
                    # the next wake-up immediately aware of what was just said.
                    try:
                        await self.flush_transcript()
                        print("💾 Conversation saved before passive mode.")
                    except Exception as e:
                        print(f"⚠️ Could not save conversation before passive mode: {e}")

                    # DO NOT set stop_event here. That would shut down SHADOW.
                    # Ask only the current Gemini session to return to passive mode.
                    self.passive_request_event.set()
                    return
            except asyncio.CancelledError:
                return

    # ========================================================
    # LOCAL EXPRESSION COMMAND DETECTION
    # ========================================================

    @staticmethod
    def detect_expression_command(text):
        """Detect a request to SHOW an expression, independent of exact wording.

        This is intentionally local: no LLM, camera, or tool call is needed.
        The detector accepts flexible English/Hinglish phrasing and a small
        multilingual vocabulary, while avoiding normal sentences such as
        "I am happy" or "why are you angry?".
        """
        if not text:
            return None

        # IMPORTANT: keep Unicode characters.  Do NOT reduce the transcript
        # to [a-z0-9], otherwise Hindi/Marathi/etc. speech disappears.
        normalized = re.sub(r"[\u200b\u200c\u200d]", " ", text.casefold())
        # Keep Unicode combining marks (Hindi matras, Bengali signs, etc.).
        # Python's \w does not include many of these marks, so using
        # `[^\w\s]` would corrupt text such as "गुस्सा हूँ".
        normalized = "".join(
            ch if (ch.isalnum() or ch.isspace() or unicodedata.category(ch).startswith("M")) else " "
            for ch in normalized
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()
        words = set(normalized.split())

        available = set(available_expressions())

        # ----------------------------------------------------
        # Expression vocabulary.
        # The English aliases cover natural speech variations.  The extra
        # terms cover common Hindi/Hinglish + major Indian-language wording.
        # More aliases can be added without changing the detection logic.
        # ----------------------------------------------------
        aliases = {
            "shadow_angry": {
                "angry", "anger", "mad", "furious", "rage", "raging",
                "gussa", "gusse", "गुस्सा", "गुस्से", "क्रोधित", "क्रोध",
                "राग", "रागावलेला", "রাগ", "রাগী", "கோபம்", "கோபமாக",
                "కోపం", "కోపంగా", "ಕೋಪ", "ಕೋಪದಿಂದ", "ഗൗരവം", "കോപം",
            },
            "shadow_happy": {
                "happy", "happiness", "smile", "smiling", "joy", "joyful",
                "cheerful", "glad", "khush", "khushi", "खुश", "खुशी", "आनंदी",
                "आनंद", "আনন্দ", "খুশি", "சந்தோஷம்", "மகிழ்ச்சி", "సంతోషం",
                "ఆనందం", "ಸಂತೋಷ", "ಸಂತೋಷವಾಗಿ", "സന്തോഷം",
            },
            "shadow_sad": {
                "sad", "sadness", "cry", "crying", "upset", "unhappy",
                "dukhi", "dukh", "उदास", "दुखी", "दुःखी", "दुःख", "उदासीन",
                "দুঃখ", "দুঃখী", "মনখারাপ", "சோகம்", "வருத்தம்", "దుఃఖం",
                "బాధ", "ದುಃಖ", "ದುಃಖಿತ", "സങ്കടം",
            },
            "shadow_confused": {
                "confused", "confusion", "puzzled", "bewildered", "uncertain",
                "uljhan", "uljha", "उलझन", "उलझा", "भ्रमित", "गोंधळ", "गोंधळलेला",
                "বিভ্রান্ত", "குழப்பம்", "குழப்பமாக", "గందరగోళం", "ಗೊಂದಲ", "ആശയക്കുഴപ്പം",
            },
            "shadow_taking": {
                "thinking", "think", "thoughtful", "contemplating", "taking",
                "soch", "socho", "सोच", "सोचता", "विचार", "विचारात", "विचारशील",
                "ভাবনা", "চিন্তা", "சிந்தனை", "யோசனை", "ఆలోచన", "ಆಲೋಚನೆ", "ചിന്ത",
            },
            "neutral": {
                "neutral", "normal", "calm", "default", "शांत", "सामान्य", "नॉर्मल",
                "শান্ত", "அமைதி", "శాంతి", "ಶಾಂತ", "ശാന്തം",
            },
            "mentor": {"mentor", "teacher", "teach", "गुरु", "शिक्षक", "शिक्षिका", "teacher"},
        }

        # Support any future GIF in gifs2 by its filename too.
        for gif_name in available:
            stem = gif_name.replace(".gif", "")
            clean = stem.removeprefix("shadow_").replace("_", " ").casefold()
            if clean and clean in normalized:
                aliases.setdefault(gif_name, set()).add(clean)

        # ----------------------------------------------------
        # Request intent.
        # We deliberately accept MANY ways of asking, not one sentence.
        # ----------------------------------------------------
        request_terms = {
            # English
            "show", "display", "make", "give", "do", "have", "change",
            "switch", "use", "wear", "put", "be", "become", "look", "looklike",
            "see", "want", "wish", "can", "could", "please", "let", "try",
            "expression", "express", "face", "facial", "appear", "appearances",
            # Hindi / Hinglish
            "dikhao", "dikha", "dikhana", "dikhaye", "banao", "bana", "banake",
            "dikhaao", "dikhade", "dikhado", "करो", "कर", "दिखाओ", "दिखा", "बनाओ",
            "चेहरा", "चेहरे", "भाव", "अभिव्यक्ति",
            # Marathi
            "दाखव", "दाखवा", "दाखवून", "कर", "चेहरा", "भाव",
            # Bengali
            "দেখাও", "দেখা", "বানাও", "মুখ", "অভিব্যক্তি",
            # Tamil
            "காட்டு", "காட்டுங்கள்", "செய்", "முகம்", "வெளிப்பாடு",
            # Telugu
            "చూపించు", "చూపించండి", "చేయి", "ముఖం", "భావం",
            # Kannada
            "ತೋರಿಸು", "ತೋರಿಸಿ", "ಮಾಡು", "ಮುಖ", "ಭಾವ",
            # Malayalam
            "കാണിക്കൂ", "കാണിക്കുക", "ചെയ്യൂ", "മുഖം", "ഭാവം",
        }

        # First identify the requested emotion.  This allows flexible word
        # order such as "angry face show", "show me angry", etc.
        matched_gifs = []
        for gif_name, emotion_words in aliases.items():
            if gif_name not in available:
                continue
            if any(word in words for word in emotion_words):
                matched_gifs.append(gif_name)

        if not matched_gifs:
            return None

        # Direct expression nouns are strong evidence: "angry face", "happy
        # expression", Hindi equivalents, etc.
        expression_noun_hit = any(
            term in words for term in {
                "face", "facial", "expression", "express", "look",
                "चेहरा", "चेहरे", "भाव", "अभिव्यक्ति", "মুখ", "অভিব্যক্তি",
                "முகம்", "வெளிப்பாடு", "ముఖం", "భావం", "ಮುಖ", "ಭಾವ", "മുഖം",
                "ഭാവം",
            }
        )

        request_hit = any(term in words for term in request_terms)

        # Only strong visual/request language should create a local face command.
        # Generic words such as "make", "do", "be", "kar", "mujhe", or
        # "shadow" are intentionally NOT enough because they occur naturally
        # in emotional statements like "Shadow, mujhe gussa aa raha hai".
        strong_request_terms = {
            "show", "display", "look", "see", "appear", "appearance",
            "dikhao", "dikha", "dikhana", "dikhaye", "dikhado", "dikhade",
            "dikhaao", "banao", "बनाओ", "दिखाओ", "दिखा", "दाखव", "दाखवा",
            "দেখাও", "காட்டு", "காட்டுங்கள்", "చూపించు", "చూపించండి",
            "ತೋರಿಸು", "ತೋರಿಸಿ", "കാണിക്കൂ", "കാണിക്കുക",
        }
        strong_request_hit = bool(words & strong_request_terms)

        # Natural request phrases that don't contain an explicit face noun.
        phrase_request_hit = any(
            phrase in normalized
            for phrase in (
                "i want to see you", "i wanna see you", "i want you to look",
                "i would like to see you", "can you look", "could you look",
                "would you look", "make yourself", "change your face",
                "change your expression", "give me your", "show yourself",
                "apna face", "apna chehra", "apne gusse wala face",
                "अपने गुस्से वाला चेहरा", "अपना चेहरा", "अपना फेस",
            )
        )

        # If the transcript explicitly refers to Shadow/you, an expression
        # request is also strong enough even when the exact verb is unusual.
        self_reference = any(
            term in words for term in {
                "you", "your", "yourself", "shadow", "tum", "aap", "apka", "apni",
                "तुम", "तुम्हारा", "आप", "आपका", "आपकी", "নিজের", "তুমি",
                "நீ", "உன்", "మీరు", "నీ", "ನೀನು", "ನಿಮ್ಮ", "നീ", "നിന്റെ",
            }
        )

        # A face/expression noun is strong evidence, as is a strong visual
        # request.  Do not use generic request words or self-reference alone.
        if not (expression_noun_hit or strong_request_hit or phrase_request_hit):
            return None

        return matched_gifs[0]

    # ========================================================
    # EMOTION STATEMENT DETECTION
    # ========================================================

    @staticmethod
    def detect_user_emotion(text):
        """Detect the user's emotional state for conversational expression.

        This is deliberately separate from the "show me your face" command.
        It reacts to what the user is feeling or describing, then lets Gemini
        continue the conversation normally.
        """
        if not text:
            return None

        import unicodedata

        # NFC keeps Hindi/Devanagari and other Unicode scripts intact.
        normalized = unicodedata.normalize("NFC", text.casefold())
        normalized = re.sub(r"[\u200b\u200c\u200d]", " ", normalized)
        normalized = "".join(
            ch if (ch.isalnum() or ch.isspace() or unicodedata.category(ch).startswith("M")) else " "
            for ch in normalized
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return None

        # ------------------------------------------------------------
        # 1. EXPLICIT FEELINGS — strongest signal.
        # ------------------------------------------------------------
        explicit = {
            "shadow_angry": (
                "i am angry", "i'm angry", "im angry", "i feel angry",
                "i am mad", "i'm mad", "i feel mad", "i am furious",
                "i am frustrated", "i'm frustrated", "i am annoyed",
                "mujhe gussa", "mujhe bahut gussa", "main gussa", "mai gussa",
                "mein gussa", "gussa aa raha", "gussa aa rha", "gussa hoon",
                "gussa hun", "gusse mein", "gusse me", "main naraz", "mein naraz",
                "मैं गुस्सा", "मुझे गुस्सा", "मैं बहुत गुस्सा", "गुस्से में",
                "गुस्सा हूँ", "गुस्सा हूं", "मैं नाराज़", "मैं नाराज",
            ),
            "shadow_sad": (
                "i am sad", "i'm sad", "im sad", "i feel sad", "i am very sad",
                "i'm very sad", "i feel very sad", "i am upset", "i'm upset",
                "i feel upset", "i am unhappy", "i feel lonely", "i am lonely",
                "i feel hurt", "i am hurt", "i feel terrible", "i feel awful",
                "i am heartbroken", "main sad", "mai sad", "mein sad",
                "main bahut sad", "mai bahut sad", "mein bahut sad", "sad hoon",
                "sad hun", "mujhe udaasi", "mujhe dukh", "mujhe bahut dukh",
                "main dukhi", "mai dukhi", "mein dukhi", "dukhi hoon", "dukhi hun",
                "main udaas", "mai udaas", "mein udaas", "udaas hoon", "udas hoon",
                "mood off", "mera mood off", "मुझे दुख", "मुझे उदासी", "मैं दुखी",
                "दुखी हूँ", "दुखी हूं", "मैं उदास", "उदास हूँ", "उदास हूं",
                "बहुत उदास", "मेरा मूड ऑफ", "मेरा दिल टूट", "दिल टूट गया",
            ),
            "shadow_happy": (
                "i am happy", "i'm happy", "im happy", "i feel happy", "i am very happy",
                "i'm very happy", "i feel very happy", "i am excited", "i'm excited",
                "i feel excited", "i am glad", "i'm glad", "i feel great",
                "i feel amazing", "main happy", "mai happy", "mein happy",
                "main bahut happy", "mai bahut happy", "mein bahut happy",
                "main khush", "mai khush", "mein khush", "main bahut khush",
                "mai bahut khush", "mein bahut khush", "khush hoon", "khush hun",
                "mujhe khushi", "mujhe bahut khushi", "khushi hui", "khushi ho rahi",
                "मैं खुश", "मुझे खुशी", "बहुत खुश", "खुश हूँ", "खुश हूं", "खुश हो गया",
                "खुशी हुई", "आज बहुत अच्छा लग रहा", "आज बहुत खुश",
            ),
            "shadow_confused": (
                "i am confused", "i'm confused", "im confused", "i feel confused",
                "i am very confused", "i'm very confused", "i feel lost",
                "i am puzzled", "i'm puzzled", "i don't understand", "i do not understand",
                "i can't decide", "i cannot decide", "i am stuck", "i'm stuck",
                "main confused", "mai confused", "mein confused", "main confuse",
                "mai confuse", "mein confuse", "main bahut confused", "mein bahut confused",
                "confused hoon", "confused hun", "confuse hoon", "confuse hun",
                "confused ho gaya", "confused ho gya", "confuse ho gaya", "confuse ho gya",
                "mujhe confusion", "mujhe uljhan", "main uljha", "main ulajh gaya",
                "मुझे कंफ्यूजन", "मुझे कन्फ्यूजन", "मैं कंफ्यूज", "मैं कन्फ्यूज",
                "मैं बहुत कंफ्यूज", "कंफ्यूज हूं", "कन्फ्यूज हूं", "कंफ्यूज हो गया",
                "कन्फ्यूज हो गया", "मुझे उलझन", "मैं उलझ गया", "मैं उलझ गई",
                "समझ नहीं आ रहा", "समझ नहीं आ रही", "समझ नहीं आया", "क्या करूं",
            ),
        }

        for gif_name, phrases in explicit.items():
            if any(phrase in normalized for phrase in phrases):
                return gif_name

        # ------------------------------------------------------------
        # 2. SHORT NATURAL EMOTION WORDS.
        # Only use these when the transcript is clearly conversational,
        # so a random mention like "happy face" isn't treated as the user's mood.
        # ------------------------------------------------------------
        words = set(normalized.split())
        short_emotions = {
            "shadow_angry": {
                "angry", "mad", "furious", "frustrated", "frustrating", "annoyed",
                "gussa", "गुस्सा", "गुस्से", "नाराज़", "नाराज",
            },
            "shadow_sad": {
                "sad", "upset", "unhappy", "lonely", "hurt", "heartbroken",
                "dukhi", "udaas", "udas", "दुखी", "उदास", "उदासी", "दुःखी",
            },
            "shadow_happy": {
                "happy", "excited", "glad", "joyful", "khush", "khushi",
                "खुश", "खुशी", "आनंद", "आनंदी",
            },
            "shadow_confused": {
                "confused", "confuse", "puzzled", "stuck", "lost", "uljhan",
                "उलझन", "कंफ्यूज", "कन्फ्यूज", "भ्रमित",
            },
        }

        personal_markers = {
            "i", "i'm", "im", "my", "me", "feel", "feeling", "am",
            "main", "mai", "mein", "mujhe", "mera", "meri", "mere",
            "मैं", "मुझे", "मेरा", "मेरी", "मेरे",
        }
        personal_context = bool(words & personal_markers)
        if personal_context:
            for gif_name, cues in short_emotions.items():
                if words & cues:
                    return gif_name

        # ------------------------------------------------------------
        # 3. SITUATION-BASED EMOTION.
        # This is what makes SHADOW feel personalized rather than keyword-based.
        # ------------------------------------------------------------
        situations = {
            "shadow_sad": (
                "failed my interview", "interview went badly", "interview went bad",
                "exam went badly", "exam went bad", "didn't get selected",
                "did not get selected", "job nahi mila", "select nahi hua",
                "reject ho gaya", "fail ho gaya", "breakup ho gaya", "mera breakup",
                "dil toot gaya", "dil toot", "ro raha", "ro rahi", "rona aa raha",
                "bad day", "terrible day", "awful day", "bad news", "lost my",
                "haar gaya", "haar gayi", "हार गया", "हार गई", "फेल हो गया",
                "रिजेक्ट हो गया", "दिल टूट गया", "रो रहा", "रो रही",
            ),
            "shadow_happy": (
                "got selected", "i got selected", "got the job", "got a job",
                "got an offer", "got admission", "passed my exam", "cleared my exam",
                "cleared the interview", "won the", "i won", "good news", "great news",
                "best day", "amazing day", "mera selection ho gaya", "selection ho gaya",
                "job mil gayi", "offer mil gaya", "admission mil gaya", "pass ho gaya",
                "clear ho gaya", "jeet gaya", "jeet gayi", "सेलेक्शन हो गया", "जॉब मिल गई",
                "अच्छी खबर", "खुशखबरी",
            ),
            "shadow_angry": (
                "so frustrating", "this is frustrating", "this is annoying",
                "fed up", "sick of this", "so unfair", "they lied to me",
                "he lied to me", "she lied to me", "they cheated me", "cheated on me",
                "bahut pareshan", "tang aa gaya", "tang aa gayi", "gussa dila",
                "jhoot bola", "dhokha diya", "बहुत परेशान", "तंग आ गया", "धोखा दिया",
            ),
            "shadow_confused": (
                "don't know what to do", "dont know what to do", "what should i do",
                "which one should i choose", "can't decide", "cannot decide",
                "doesn't make sense", "doesnt make sense", "i'm stuck", "im stuck",
                "samajh nahi aa raha", "samajh nahi aa rahi", "samajh nahi aaya",
                "samajh nahi aya", "kya karu", "kya karoon", "ulajh gaya", "ulajh gayi",
                "समझ नहीं आ रहा", "समझ नहीं आ रही", "क्या करूं", "उलझ गया", "उलझ गई",
            ),
        }

        for gif_name, cues in situations.items():
            if any(cue in normalized for cue in cues):
                return gif_name

        return None

    # ========================================================
    # USER TRANSCRIPT
    # ========================================================

    async def on_user_transcript(self, text):

        if not text:
            return

        text = text.strip()

        if not text:
            return

        # Keep a simple lowercase version for the existing phrase-based
        # mode controls (interview start/stop, etc.). The expression detector
        # uses the original Unicode text so multilingual commands are not lost.
        lower_text = text.casefold()

        print(
            f"\n👤 You: {text}"
        )

        self.user_transcript.append(text)
        self.last_user_activity = time.monotonic()

        # ----------------------------------------------------
        # HEAD MOVEMENT: LOCAL RASPBERRY PI -> ESP32 COMMAND
        # ----------------------------------------------------
        # Handle physical head movement before Gemini/camera tools.
        # "see left/right/front" is a local command and should not
        # wait for Gemini or invoke the camera.
        # Run the blocking HTTP call in a worker thread so the asyncio event
        # loop can continue handling Gemini Live/audio tasks while the ESP32 replies.
        # DEBUG: confirm that the voice transcript reaches the head controller.
        print(f"🎤 HEAD DEBUG — transcript: {text!r}")
        head_direction = await asyncio.to_thread(move_head_from_voice, text)
        print(f"🎯 HEAD DEBUG — detected direction: {head_direction!r}")
        if head_direction:
            print(f"🤖 HEAD COMMAND → {head_direction}")
            return True

        # ----------------------------------------------------
        # EXPRESSION: LOCAL VOICE COMMAND
        # ----------------------------------------------------
        # Expression requests are handled locally. Gemini does NOT need to
        # call the camera or understand the GIF filenames.
        requested_gif = self.detect_expression_command(text)

        if requested_gif:
            # Expression requests are temporary visual commands.
            # Show the requested GIF for exactly 5 seconds, then return to
            # neutral. Do not keep the expression as persistent state.
            self.requested_expression = "neutral"
            self.manual_expression_active = False
            self.expression_command_active = True

            if self.expression_reset_task and not self.expression_reset_task.done():
                self.expression_reset_task.cancel()

            expression_management(requested_gif)
            print(f"🎭 VOICE EXPRESSION COMMAND → {requested_gif}")

            async def reset_expression_after_delay():
                try:
                    await asyncio.sleep(5)
                    expression_management("neutral")
                    self.expression_command_active = False
                    print("🎭 Expression preview finished → neutral")
                except asyncio.CancelledError:
                    pass

            self.expression_reset_task = asyncio.create_task(
                reset_expression_after_delay()
            )

            # LOCAL-ONLY command: suppress Gemini's response, audio and tools
            # for this turn. The user asked SHADOW to show a face, not explain it.
            return True

        # ----------------------------------------------------
        # USER EMOTION: VISUAL RESPONSE + NORMAL CONVERSATION
        # ----------------------------------------------------
        # Example: "I am very angry". Show the matching face, but DO NOT
        # suppress Gemini. SHADOW should still talk with the user normally.
        user_emotion_gif = self.detect_user_emotion(text)
        if user_emotion_gif:
            self.expression_command_active = False
            self.emotion_expression_active = True
            self.manual_expression_active = False

            if self.expression_reset_task and not self.expression_reset_task.done():
                self.expression_reset_task.cancel()

            expression_management(user_emotion_gif)
            print(f"🎭 USER EMOTION → {user_emotion_gif}")

            async def reset_emotion_after_delay():
                try:
                    # Keep the emotional face visible while SHADOW responds.
                    # The turn-complete callback normally clears it; this is
                    # only a safety timeout if a Live turn gets stuck.
                    await asyncio.sleep(12)
                    if self.emotion_expression_active:
                        expression_management("neutral")
                        self.emotion_expression_active = False
                        print("🎭 Emotion safety timeout → neutral")
                except asyncio.CancelledError:
                    pass

            self.expression_reset_task = asyncio.create_task(
                reset_emotion_after_delay()
            )
            # Return False: Gemini continues speaking normally.

        interview_start_phrases = (
            "start mock interview",
            "mock interview",
            "interview me",
            "take my technical interview",
            "take my hr interview",
            "practice interview",
        )

        interview_stop_phrases = (
            "stop interview",
            "end interview",
            "finish interview",
            "exit interview",
        )

        if any(phrase in lower_text for phrase in interview_stop_phrases):
            self.interview_mode = False
            self.manual_expression_active = False
            self.requested_expression = "neutral"
            self.expression_command_active = False
            expression_management("neutral")

        elif any(phrase in lower_text for phrase in interview_start_phrases):
            self.interview_mode = True
            self.manual_expression_active = False
            self.expression_command_active = False
            self.requested_expression = "mentor"
            expression_management("mentor")
            print("🎤 Mock interview mode started - mentor expression active.")

        return False

    # ========================================================
    # ASSISTANT TRANSCRIPT
    # ========================================================

    async def on_assistant_transcript(self, text):

        if not text:
            return

        text = text.strip()

        if not text:
            return

        print(
            f"\n🤖 Shadow: {text}"
        )

        self.assistant_transcript.append(text)

    # ========================================================
    # TURN COMPLETE
    # ========================================================

    async def on_turn_complete(self):
        await self.flush_transcript()

        # A touch event can start a short Gemini "Ouch!" turn. Do not let
        # this normal turn-complete handler immediately replace shadow_happy
        # with neutral while the touch sensor's 2-second expression timer runs.
        if self.touch_expression_active:
            return

        # A reminder delivery is complete once Gemini finishes the turn.
        # Clear the guard so normal user reminder requests work immediately.
        if self.reminder_delivery_active:
            self.reminder_delivery_active = False
            print("🔔 Reminder delivery finished.")
            expression_management("neutral")
            return

        # audio_io.py temporarily changes the face to "speaking" while
        # Gemini is producing audio. Restore the user's requested GIF after
        # the turn completes.
        if self.interview_mode:
            expression_management("mentor")
        elif self.emotion_expression_active:
            # User-emotion mode: keep the matching face during Shadow's reply,
            # then return to neutral when the response is finished.
            expression_management("neutral")
            self.emotion_expression_active = False
            if self.expression_reset_task and not self.expression_reset_task.done():
                self.expression_reset_task.cancel()
            print("🎭 User emotion response finished → neutral")
        elif not self.expression_command_active:
            expression_management("neutral")

        # For an expression command, the 5-second timer owns the reset.
        # Do not clear expression_command_active here or the turn-complete
        # callback could force an early neutral transition.
        if not self.expression_command_active:
            self.expression_command_active = False

    # ========================================================
    # FLUSH TRANSCRIPT
    # ========================================================

    async def flush_transcript(self):

        # ----------------------------------------------------
        # USER
        # ----------------------------------------------------

        if self.user_transcript:

            text = " ".join(
                self.user_transcript
            ).strip()

            if text:
                try:
                    log_turn(
                        self.conn,
                        self.session_id,
                        "user",
                        text,
                    )

                except Exception as e:
                    print(
                        f"\n⚠️ Failed to log user turn: {e}"
                    )

            self.user_transcript.clear()

        # ----------------------------------------------------
        # ASSISTANT
        # ----------------------------------------------------

        if self.assistant_transcript:

            text = " ".join(
                self.assistant_transcript
            ).strip()

            if text:
                try:
                    log_turn(
                        self.conn,
                        self.session_id,
                        "assistant",
                        text,
                    )

                except Exception as e:
                    print(
                        f"\n⚠️ Failed to log assistant turn: {e}"
                    )

            self.assistant_transcript.clear()

    # ========================================================
    # TOOL CALL
    # ========================================================

    async def on_tool_call(
        self,
        function_calls,
        session=None,
    ):

        async with self.tool_lock:

            self.tool_event.set()

            responses = []

            try:

                for function_call in function_calls:

                    name = function_call.name

                    args = (
                        function_call.args
                        or {}
                    )

                    print()
                    print(
                        f"🛠️ Tool: {name}"
                    )
                    print(
                        f"   Args: {args}"
                    )

                    started = asyncio.get_running_loop().time()

                    try:
                        # ------------------------------------------------
                        # CAMERA / VISUAL TOOL
                        # ------------------------------------------------
                        if name == "look_at_camera" and self.expression_command_active:
                            result = {
                                "success": True,
                                "handled_locally": True,
                                "message": (
                                    "The user's expression request was already handled locally. "
                                    "Do not use the camera and do not claim that SHADOW cannot show expressions."
                                ),
                            }
                        elif name == "look_at_camera":
                            if session is None:
                                raise RuntimeError("Camera tool requires an active Gemini Live session.")
                            frame = await asyncio.to_thread(capture_frame)
                            await session.send_realtime_input(
                                video=types.Blob(data=frame, mime_type="image/jpeg")
                            )
                            result = {
                                "success": True,
                                "camera_frame_attached": True,
                                "instruction": "Inspect the attached current camera frame and answer the user's visual question. Identify the main object briefly and do not guess if it is unclear.",
                            }
                        # ------------------------------------------------
                        # REMINDER DELIVERY GUARD
                        # ------------------------------------------------
                        # SQLite has already created/scheduled this reminder.
                        # Gemini receives the reminder as internal input so
                        # it can speak it naturally. If Gemini nevertheless
                        # tries to call create_reminder, do NOT create a
                        # second reminder. Tell Gemini to announce the
                        # already-scheduled reminder instead.
                        elif (
                            self.reminder_delivery_active
                            and name == "create_reminder"
                        ):
                            result = {
                                "success": True,
                                "already_scheduled": True,
                                "message": (
                                    "This reminder was already scheduled by "
                                    "the reminder system. Do not create another "
                                    "reminder. Simply announce the reminder to "
                                    "the user naturally and briefly."
                                ),
                            }
                        else:
                            result = (
                                await self.execute_tool(
                                    name,
                                    args,
                                )
                            )

                    except Exception as e:

                        print(
                            f"\n❌ Tool '{name}' failed:"
                        )

                        print(
                            f"   {type(e).__name__}: {e}"
                        )

                        result = {
                            "success": False,
                            "error": str(e),
                        }

                    elapsed = (
                        asyncio.get_running_loop().time()
                        - started
                    )

                    print(
                        f"   ⏱️ Tool time: {elapsed:.2f}s"
                    )

                    # ----------------------------------------
                    # LOG TOOL
                    # ----------------------------------------

                    try:
                        log_turn(
                            self.conn,
                            self.session_id,
                            "tool",
                            (
                                f"{name}: "
                                f"{result}"
                            ),
                        )

                    except Exception as e:
                        print(
                            f"\n⚠️ Tool logging failed: {e}"
                        )

                    # ----------------------------------------
                    # FUNCTION RESPONSE
                    # ----------------------------------------

                    responses.append(
                        types.FunctionResponse(
                            id=function_call.id,
                            name=name,
                            response=result,
                        )
                    )

                return responses

            finally:
                self.tool_event.clear()

    # ========================================================
    # TOOL EXECUTION
    # ========================================================

    async def execute_tool(
        self,
        name,
        args,
    ):

        # ====================================================
        # GPT-5
        # ====================================================

        if name == "ask_gpt5":

            return await asyncio.to_thread(
                args.get(
                    "task",
                    "",
                ),
                args.get(
                    "context",
                    "",
                ),
            )

        # ====================================================
        # WEATHER
        # ====================================================

        if name == "get_weather":
            expression_management("loading")
            return await asyncio.to_thread(
                get_weather,
                args.get(
                    "city",
                    "",
                ),
            )

        # ====================================================
        # TIME
        # ====================================================

        if name == "get_time":
            return get_time()

        # ====================================================
        # SAVE MEMORY
        # ====================================================

        if name == "save_user_fact":

            return save_user_fact(
                self.conn,
                args.get(
                    "fact",
                    "",
                ),
                args.get(
                    "category",
                    "other",
                ),
            )

        # ====================================================
        # WEB SEARCH
        # ====================================================

        if name == "search_web":

            return await asyncio.to_thread(
                search_web,
                args.get(
                    "query",
                    "",
                ),
            )

        # ====================================================
        # QUIZ START
        # ====================================================

        if name == "start_quiz":

            return start_quiz(
                args.get(
                    "question",
                    "",
                ),
                args.get(
                    "option_a",
                    "",
                ),
                args.get(
                    "option_b",
                    "",
                ),
                args.get(
                    "option_c",
                    "",
                ),
                args.get(
                    "option_d",
                    "",
                ),
                args.get(
                    "correct_answer",
                    "",
                ),
                args.get(
                    "explanation",
                    "",
                ),
            )

        # ====================================================
        # QUIZ ANSWER
        # ====================================================

        if name == "check_quiz_answer":

            return check_quiz_answer(
                args.get(
                    "user_answer",
                    "",
                )
            )

        # ====================================================
        # STUDY COACH
        # ====================================================

        if name == "start_study_session":
            return await asyncio.to_thread(
                start_study_session,
                args.get("topic", "Study"),
                args.get("focus_minutes", 25),
                args.get("break_minutes", 5),
                args.get("cycles", 4),
            )

        if name == "pause_study_session":
            return await asyncio.to_thread(pause_study_session)

        if name == "resume_study_session":
            return await asyncio.to_thread(resume_study_session)

        if name == "stop_study_session":
            return await asyncio.to_thread(stop_study_session)

        if name == "get_study_status":
            return await asyncio.to_thread(get_study_status)

        if name == "get_study_progress":
            return await asyncio.to_thread(get_study_progress, args.get("days", 1))

        # ====================================================
        # CREATE REMINDER
        # ====================================================

        if name == "create_reminder":

            result = await asyncio.to_thread(
                create_reminder,
                args.get("title", ""),
                args.get("scheduled_at", ""),
                args.get("delay_seconds", 0),
                args.get("priority", "normal"),
                args.get("notification_channel", "shadow"),
            )

            # Remember the ID so "cancel that reminder" can work
            # without Gemini needing to know the UUID.
            if result.get("success") and result.get("reminder_id"):
                self.last_reminder_id = result["reminder_id"]
                self.last_reminder_title = result.get(
                    "title",
                    args.get("title", ""),
                )

            return result

        # ====================================================
        # CANCEL REMINDER
        # ====================================================

        if name == "cancel_reminder":

            reminder_id = str(
                args.get("reminder_id", "") or ""
            ).strip()

            # If Gemini doesn't provide an ID, use the latest
            # reminder created during this SHADOW session.
            if not reminder_id:
                reminder_id = self.last_reminder_id or ""

            if not reminder_id:
                return {
                    "success": False,
                    "error": (
                        "There is no recent reminder available to cancel."
                    ),
                }

            result = await asyncio.to_thread(
                cancel_reminder,
                reminder_id,
            )

            if result.get("success"):
                if reminder_id == self.last_reminder_id:
                    self.last_reminder_id = None
                    self.last_reminder_title = None

            return result


        # ====================================================
        # UNKNOWN TOOL
        # ====================================================

        return {
            "success": False,
            "error": (
                f"Unknown tool: {name}"
            ),
        }

    # ========================================================
    # LOCAL PASSIVE ACK
    # ========================================================

    async def _ack_and_signal(self, done_event):
        try:
            await self.speak_passive_ack()
        finally:
            done_event.set()

    async def speak_passive_ack(self):
        """Give an immediate local acknowledgement before Gemini connects."""
        if pyttsx3 is None:
            print("🤖 Shadow: Yes bro, I'm listening...")
            return

        def _speak():
            engine = pyttsx3.init()
            engine.setProperty("rate", 175)
            engine.say("Yes bro, I am listening.")
            engine.runAndWait()
            engine.stop()

        try:
            await asyncio.to_thread(_speak)
        except Exception as e:
            print(f"⚠️ Local acknowledgement failed: {e}")
            print("🤖 Shadow: Yes bro, I'm listening...")

    # ========================================================
    # PASSIVE REMINDER WATCHER
    # ========================================================

    async def passive_reminder_watcher(self, wake_event):
        """Watch SQLite while SHADOW is passive.

        If a reminder becomes due while Gemini Live is OFF, wake the
        application and let the normal active-session reminder worker
        announce it. This keeps reminders working without sending
        microphone audio to Gemini during passive listening.
        """
        while not self.stop_event.is_set() and not wake_event.is_set():
            try:
                due_reminders = await asyncio.to_thread(self._get_due_reminders)

                if due_reminders:
                    for reminder in due_reminders:
                        (
                            _db_id,
                            reminder_id,
                            title,
                            _scheduled_at,
                            _priority,
                            _notification_channel,
                            _status,
                        ) = reminder

                        current_status = await asyncio.to_thread(
                            self._get_reminder_status, reminder_id
                        )
                        if current_status != "active":
                            continue

                        self.last_reminder_id = reminder_id
                        self.last_reminder_title = str(title or "You have a reminder.").strip()
                        self.pending_reminder_text = (
                            "INTERNAL REMINDER DELIVERY. This reminder has ALREADY been scheduled. "
                            "Do not create, edit, cancel, snooze, or reschedule it. "
                            "Simply announce it naturally and briefly to the user. "
                            f"Reminder: {self.last_reminder_title}"
                        )
                        self.activation_reason = "reminder"

                        # Expression: reminder is being delivered.
                        # The GIF is named remainder.gif in the project.
                        expression_management("remainder")

                        print()
                        print("🔔 Reminder is due while SHADOW is passive.")
                        print(f"   Title: {self.last_reminder_title}")
                        print("🔥 Activating SHADOW to announce the reminder...")

                        wake_event.set()
                        return

                await asyncio.sleep(0.25)

            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"❌ Passive reminder watcher error: {e}")
                await asyncio.sleep(0.5)

    # ========================================================
    # TOUCH SENSOR / GEMINI SESSION BRIDGE
    # ========================================================

    def _start_touch_sensor(self, session):
        """Create and start GPIO-16 touch sensor for the active Live session."""
        self._stop_touch_sensor()

        try:
            loop = asyncio.get_running_loop()
            self.touch_sensor = ShadowTouchSensor(
                session=session,
                loop=loop,
            )
            self.touch_sensor.on_touch_start = self._on_touch_expression_start
            self.touch_sensor.on_touch_end = self._on_touch_expression_end
            started = self.touch_sensor.start()
            if started:
                print("👆 Touch sensor ready on GPIO 16.")
            else:
                print("⚠️ Touch sensor did not start; check GPIO/gpiozero.")
        except Exception as exc:
            self.touch_sensor = None
            print(f"⚠️ Could not start touch sensor: {exc}")

    def _on_touch_expression_start(self):
        """Give the touch reaction priority over normal expression cleanup."""
        self.touch_expression_active = True

    def _on_touch_expression_end(self):
        """Release touch-expression priority when the touch reaction ends."""
        self.touch_expression_active = False

    def _stop_touch_sensor(self):
        """Stop GPIO touch detection safely before closing the Live session."""
        sensor = getattr(self, "touch_sensor", None)
        if sensor is not None:
            try:
                sensor.stop()
            except Exception as exc:
                print(f"⚠️ Touch sensor cleanup warning: {exc}")
            finally:
                self.touch_sensor = None

    # ========================================================
    # RUN SESSION
    # ========================================================

    async def _run_cycle(self):

        # ----------------------------------------------------
        # LOAD MEMORY ONCE PER APPLICATION
        # ----------------------------------------------------
        # The Shadow object stays alive across ACTIVE <-> PASSIVE.
        # We load long-term memory once, while recent conversation is
        # refreshed before every Gemini session so the new Live session
        # always receives the latest persisted context.
        if not self.memory_initialized:
            print("\n🧠 Loading long-term memory...")
            try:
                memory = load_long_term_memory_summary(self.conn)
            except Exception as e:
                print(f"⚠️ Memory loading failed: {e}")
                memory = ""
            self.memory_initialized = True
        else:
            print("\n🧠 Long-term memory already loaded.")
            try:
                memory = load_long_term_memory_summary(self.conn)
            except Exception as e:
                print(f"⚠️ Memory refresh failed: {e}")
                memory = ""

        print("💬 Loading recent conversation...")
        try:
            recent_conversation = get_recent_conversation_text(
                self.conn,
                limit=MAX_RECENT_CONVERSATION_TURNS,
            )
        except Exception as e:
            print(f"⚠️ Recent conversation loading failed: {e}")
            recent_conversation = ""

        if recent_conversation:
            print("💬 Previous conversation context loaded.")
        else:
            print("💬 No previous conversation found.")

        print("🧠 Updating Shadow instructions from persistent memory...")
        instructions = build_instructions(memory, recent_conversation)

        self.passive_request_event.clear()
        self.idle_timeout_triggered = False
        self.passive_mode = False

        # ----------------------------------------------------
        # DIRECT STARTUP
        # ----------------------------------------------------
        # Wake-word detection has been removed. SHADOW enters Gemini Live
        # immediately when main.py starts. Reminders remain active inside
        # the Live session.
        self.passive_mode = False
        self.activation_reason = "startup"
        self.idle_timeout_triggered = False
        self.last_user_activity = time.monotonic()
        print("\n🟢 DIRECT MODE")
        print("🎤 SHADOW is opening Gemini Live immediately.")
        print("🔔 Reminder watcher will run in the active session.")

        # ----------------------------------------------------
        # GEMINI CLIENT
        # ----------------------------------------------------

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        # ----------------------------------------------------
        # LIVE CONFIG
        # ----------------------------------------------------

        config = types.LiveConnectConfig(

            response_modalities=[
                "AUDIO"
            ],

            system_instruction=(
                types.Content(
                    parts=[
                        types.Part(
                            text=instructions
                        )
                    ]
                )
            ),

            tools=GEMINI_TOOLS,

            speech_config=types.SpeechConfig(

                voice_config=(
                    types.VoiceConfig(

                        prebuilt_voice_config=(
                            types.PrebuiltVoiceConfig(
                                voice_name=(
                                    GEMINI_VOICE
                                )
                            )
                        )
                    )
                )
            ),

            # ------------------------------------------------
            # TRANSCRIPTION
            # ------------------------------------------------

            input_audio_transcription={},
            output_audio_transcription={},

            # ------------------------------------------------
            # VAD
            # ------------------------------------------------

            realtime_input_config=(
                types.RealtimeInputConfig(

                    automatic_activity_detection=(
                        types.AutomaticActivityDetection(

                            disabled=False,

                            prefix_padding_ms=(
                                PREFIX_PADDING_MS
                            ),

                            silence_duration_ms=(
                                SILENCE_DURATION_MS
                            ),
                        )
                    )
                )
            ),

            # ------------------------------------------------
            # LOW THINKING
            # ------------------------------------------------

            thinking_config=(
                types.ThinkingConfig(
                    thinking_level="minimal"
                )
            ),
        )

        # ----------------------------------------------------
        # CREATE PLAYER
        # ----------------------------------------------------

        player = AudioPlayer()

        # ----------------------------------------------------
        # UI
        # ----------------------------------------------------

        print()
        print("=" * 60)
        print(
            "                 SHADOW"
        )
        print("=" * 60)
        print()

        print(
            "🎙️ Voice      : Gemini Live"
        )

        print(
            "🧠 Brain      : Gemini Live + GPT-5 (deep reasoning only)"
        )

        print(
            "🎓 Mentor     : Realtime conversational teaching"
        )

        print(
            "🎤 Interview  : Realtime technical / HR practice"
        )

        print(
            "🗃️ Memory     : SQLite"
        )

        print(
            "💬 Context    : Recent conversation + long-term memory"
        )

        print(
            "🌐 Web        : Tavily"
        )

        print(
            "🌤️ Weather    : Open-Meteo"
        )

        print()

        print(
            "⚡ Low-latency realtime mode"
        )

        print(
            "🔇 Echo protection enabled"
        )

        print(
            "🧠 Long-term memory enabled"
        )

        print(
            "💬 Conversation memory enabled"
        )

        print(
            "🛠️ Tools enabled"
        )

        print(
            "⏰ Reminders  : Python + SQLite"
        )

        print(
            "📚 Study Coach: Pomodoro + SQLite tracking"
        )

        print()

        print(
            "🎤 Start talking..."
        )

        print(
            "🛑 Ctrl+C to stop"
        )

        print()

        # ====================================================
        # GEMINI LIVE CONNECTION
        # ====================================================

        try:

            while not self.stop_event.is_set():

                # A GoAway means only the current Live socket is
                # being retired. Keep SHADOW alive and replace it.
                self.restart_event.clear()

                # After the first connection, rebuild only the system
                # context from the latest stored turns. This keeps a
                # replacement Live session aware of the conversation
                # without adding work to normal realtime turns.
                if "reconnect_count" in locals() and reconnect_count > 0:
                    try:
                        await self.flush_transcript()

                        recent_conversation = (
                            get_recent_conversation_text(
                                self.conn,
                                limit=MAX_RECENT_CONVERSATION_TURNS,
                            )
                        )

                        config.system_instruction = types.Content(
                            parts=[
                                types.Part(
                                    text=build_instructions(
                                        memory,
                                        recent_conversation,
                                    )
                                )
                            ]
                        )

                    except Exception as context_error:
                        print(
                            "\n⚠️ Reconnect context refresh failed: "
                            f"{context_error}"
                        )

                if "reconnect_count" not in locals():
                    reconnect_count = 0

                try:

                    print(
                        "🔌 Connecting to Gemini Live..."
                    )
                

                    async with client.aio.live.connect(
                        model=GEMINI_LIVE_MODEL,
                        config=config,
                    ) as session:

                        print(
                            "✅ Gemini Live connected."
                        )
                        self.live_session = session

                        # GPIO-16 touch is now safe to start because an
                        # active Gemini Live session is available for the
                        # Gemini-generated "Ouch!" reaction.
                        self._start_touch_sensor(session)

                        # -------------------------------------------------
                        # PASSIVE REMINDER DELIVERY
                        # -------------------------------------------------
                        # If SQLite woke SHADOW while it was passive, the
                        # reminder was already due before this Live session
                        # existed. Do not rely on the normal reminder worker
                        # to rediscover it; deliver it explicitly now.
                        if (
                            self.activation_reason == "reminder"
                            and self.pending_reminder_text
                        ):
                            try:
                                self.reminder_delivery_active = True
                                print("🔔 Delivering pending reminder to Gemini...")
                                await session.send_realtime_input(
                                    text=self.pending_reminder_text
                                )
                                if self.last_reminder_id:
                                    await asyncio.to_thread(
                                        self._mark_reminder_delivered,
                                        self.last_reminder_id,
                                    )
                                print("✅ Pending reminder sent to Gemini.")
                            except Exception as reminder_error:
                                self.reminder_delivery_active = False
                                print(
                                    "❌ Pending reminder delivery failed: "
                                    f"{reminder_error}"
                                )
                            finally:
                                self.pending_reminder_text = None

                        print()

                        # =================================================
                        # TOOL HANDLER
                        # =================================================

                        async def handle_tools(
                            function_calls
                        ):

                            try:

                                responses = (
                                    await self.on_tool_call(
                                        function_calls,
                                        session=session,
                                    )
                                )

                                if responses:

                                    await session.send_tool_response(
                                        function_responses=(
                                            responses
                                        )
                                    )

                                    print(
                                        "✅ Tool response sent."
                                    )

                            except Exception as e:

                                print(
                                    "\n❌ Tool response error:"
                                )

                                print(
                                    f"{type(e).__name__}: {e}"
                                )

                                traceback.print_exc()

                        # =================================================
                        # MICROPHONE TASK
                        # =================================================

                        mic_task = asyncio.create_task(

                            microphone(

                                session=session,

                                stop_event=(
                                    self.stop_event
                                ),

                                speaking_event=(
                                    self.speaking_event
                                ),

                                tool_event=(
                                    self.tool_event
                                ),

                                restart_event=(
                                    self.restart_event
                                ),
                            ),

                            name="microphone",
                        )

                        # =================================================
                        # RECEIVER TASK
                        # =================================================

                        receiver_task = asyncio.create_task(

                            receive_audio(

                                session=session,

                                player=player,

                                stop_event=(
                                    self.stop_event
                                ),

                                speaking_event=(
                                    self.speaking_event
                                ),

                                tool_event=(
                                    self.tool_event
                                ),

                                on_user_transcript=(
                                    self.on_user_transcript
                                ),

                                on_assistant_transcript=(
                                    self.on_assistant_transcript
                                ),

                                on_turn_complete=(
                                    self.on_turn_complete
                                ),

                                on_tool_call=(
                                    handle_tools
                                ),

                                is_emotion_expression_active=(
                                    lambda: self.emotion_expression_active
                                ),

                                restart_event=(
                                    self.restart_event
                                ),
                            ),

                            name="gemini-receiver",
                        )

                        # =================================================
                        # SQLITE REMINDER WORKER
                        # =================================================

                        reminder_task = asyncio.create_task(
                            self.reminder_worker(session),
                            name="reminder-worker",
                        )

                        study_task = asyncio.create_task(
                            self.study_worker(session),
                            name="study-worker",
                        )

                        inactivity_task = asyncio.create_task(
                            asyncio.sleep(365 * 24 * 60 * 60),
                            name="active-timeout-disabled",
                        )

                        # =================================================
                        # MONITOR TASKS
                        # =================================================

                        print(
                            "🟢 Audio tasks started."
                        )

                        print()

                        try:

                            while not self.stop_event.is_set():

                                if self.passive_request_event.is_set():
                                    break

                                if mic_task.done():

                                    print()
                                    print(
                                        "⚠️ Microphone task ended."
                                    )

                                    try:
                                        mic_task.result()

                                    except asyncio.CancelledError:
                                        print(
                                            "🛑 Microphone was cancelled."
                                        )

                                    except Exception as e:

                                        print(
                                            "❌ Microphone exception:"
                                        )

                                        print(
                                            f"{type(e).__name__}: {e!r}"
                                        )

                                        if is_gemini_goaway_error(e):
                                            self.restart_event.set()
                                            print(
                                                "🔄 GoAway detected from microphone task."
                                            )

                                        traceback.print_exc()

                                    break

                                if receiver_task.done():

                                    print()
                                    print(
                                        "⚠️ Gemini receiver task ended."
                                    )

                                    try:

                                        result = (
                                            receiver_task.result()
                                        )

                                        print(
                                            "Receiver returned:",
                                            repr(result),
                                        )

                                    except asyncio.CancelledError:

                                        print(
                                            "🛑 Gemini receiver cancelled."
                                        )

                                    except Exception as e:

                                        print(
                                            "\n❌ GEMINI RECEIVER EXCEPTION"
                                        )

                                        print(
                                            "Exception type:",
                                            type(e).__name__,
                                        )

                                        print(
                                            "Exception:",
                                            repr(e),
                                        )

                                        if is_gemini_goaway_error(e):
                                            self.restart_event.set()
                                            print(
                                                "🔄 GoAway detected from receiver task."
                                            )

                                        traceback.print_exc()

                                    break

                                await asyncio.sleep(
                                    0.05
                                )

                        except asyncio.CancelledError:

                            print(
                                "\n🛑 Session monitor cancelled."
                            )

                            raise

                        finally:

                            # Stop GPIO touch callbacks before the Live
                            # session is closed/replaced.
                            self._stop_touch_sensor()

                            print(
                                "\n🧹 Cleaning up audio tasks..."
                            )

                            if (
                                not self.restart_event.is_set()
                                and not self.passive_request_event.is_set()
                            ):
                                self.stop_event.set()

                            if not mic_task.done():
                                mic_task.cancel()

                            if not receiver_task.done():
                                receiver_task.cancel()

                            if not reminder_task.done():
                                reminder_task.cancel()

                            if not study_task.done():
                                study_task.cancel()

                            if not inactivity_task.done():
                                inactivity_task.cancel()

                            await asyncio.gather(
                                mic_task,
                                receiver_task,
                                reminder_task,
                                study_task,
                                inactivity_task,
                                return_exceptions=True,
                            )

                            print(
                                "✅ Audio tasks cleaned up."
                            )


                except asyncio.CancelledError:

                    print(
                        "\n🛑 Gemini session cancelled."
                    )

                    raise

                except Exception as e:

                    if (
                        self.restart_event.is_set()
                        or is_gemini_goaway_error(e)
                    ):
                        print(
                            "\n🔄 Gemini Live session expired."
                        )
                        print(
                            "   Reconnecting automatically..."
                        )

                        self.restart_event.clear()

                        # Persist the latest completed turns before
                        # creating the replacement Live session.
                        try:
                            await self.flush_transcript()
                        except Exception as flush_error:
                            print(
                                "\n⚠️ Transcript flush during reconnect "
                                f"failed: {flush_error}"
                            )

                        # Give the old WebSocket a moment to finish
                        # closing before opening the replacement.
                        reconnect_count += 1
                        await asyncio.sleep(0.25)
                        continue

                    print()
                    print(
                        "❌ GEMINI LIVE CONNECTION ERROR"
                    )

                    print(
                        "Exception type:",
                        type(e).__name__,
                    )

                    print(
                        "Exception:",
                        repr(e),
                    )

                    traceback.print_exc()
                    break


        finally:

            self.live_session = None
            self.speaking_event.clear()
            self.tool_event.clear()

            self.session_alive = not self.stop_event.is_set()

            self.speaking_event.clear()
            self.tool_event.clear()

            player.clear()
            player.stop()

            print(
                "🔌 Gemini session closed."
            )

    # ========================================================
    # STOP
    # ========================================================

    def stop(self):

        if not self.stop_event.is_set():
            
            print(
                "\n🛑 Stopping Shadow..."
            )

            self.stop_event.set()
            self.passive_request_event.set()


    # ========================================================
    # SHADOW LIFETIME LOOP
    # ========================================================

    async def run(self):
        """Run SHADOW directly in Gemini Live without a wake-word stage."""
        self.restart_event.clear()
        self.passive_request_event.clear()
        self.activation_reason = "startup"
        self.pending_reminder_text = None
        self.idle_timeout_triggered = False
        self.passive_mode = False
        self.session_alive = True
        await self._run_cycle()


# ============================================================
# MEMORY CONSOLIDATION
# ============================================================

async def consolidate_memory(
    conn,
):

    print(
        "\n🧠 Consolidating session memory..."
    )

    try:

        conversation = (
            get_recent_conversation_text(
                conn,
                limit=1000,
            )
        )

    except Exception as e:

        print(
            f"⚠️ Could not load conversation: {e}"
        )

        return

    if not conversation:

        print(
            "ℹ️ No conversation to consolidate."
        )

        return

    try:

        await asyncio.to_thread(
            consolidate_session_memory,
            conn,
            conversation,
        )

        print(
            "✅ Memory consolidated."
        )

    except Exception as e:

        print(
            "\n⚠️ Memory consolidation failed:"
        )

        print(
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()


# ============================================================
# MAIN
# ============================================================

async def main():

    print()
    print("🚀 Starting SHADOW...")
    gui_thread = threading.Thread(target=expression_window, daemon=True)
    gui_thread.start()
    expression_management("neutral")

    try:
        conn = sqlite3.connect(
            DB_PATH,
            check_same_thread=False,
        )
        init_db(conn)
        print("🗃️ SQLite database ready.")
    except Exception as e:
        print("\n❌ Database initialization failed:")
        print(f"{type(e).__name__}: {e}")
        return

    # IMPORTANT:
    # Keep ONE Shadow object for the whole application lifetime.
    # We only restart the Gemini Live connection when going between
    # ACTIVE and PASSIVE. This avoids throwing away SHADOW's in-memory
    # state and makes the lifecycle predictable.
    shadow = Shadow(conn)

    try:
        # run() now owns the complete ACTIVE <-> PASSIVE lifecycle.
        # main() must not rebuild/re-enter the controller after every timeout.
        await shadow.run()

    except KeyboardInterrupt:
        print("\n🛑 Keyboard interrupt.")
        shadow.stop()

    except asyncio.CancelledError:
        print("\n🛑 Main task cancelled.")
        shadow.stop()
        raise

    except Exception as e:
        print("\n❌ SHADOW FATAL ERROR")
        print("Exception type:", type(e).__name__)
        print("Exception:", repr(e))
        traceback.print_exc()

    finally:
        shadow.stop()
        shadow._stop_touch_sensor()

        try:
            await shadow.flush_transcript()
        except Exception as e:
            print(f"⚠️ Final transcript flush failed: {e}")

        await consolidate_memory(conn)

        try:
            conn.commit()
        except Exception:
            pass

        try:
            conn.close()
        except Exception:
            pass

        print()
        print("=" * 60)
        print("👋 SHADOW CLOSED")
        print("=" * 60)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "\n👋 Shadow stopped by user."
        )



