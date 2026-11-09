import sqlite3
from typing import Optional


def init_db(conn: sqlite3.Connection):

    cursor = conn.cursor()

    # ========================================================
    # LONG TERM MEMORY
    # ========================================================

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            category TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # ========================================================
    # CONVERSATIONS
    # ========================================================

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            item_id TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    _init_reminder_tables(conn)
    _init_study_tables(conn)

    conn.commit()


def save_fact(
    conn: sqlite3.Connection,
    fact: str,
    category: str,
):

    fact = fact.strip()
    category = category.strip()

    if not fact:
        return

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM long_term_memory
        WHERE fact = ?
        LIMIT 1
        """,
        (fact,),
    )

    if cursor.fetchone():
        return

    cursor.execute(
        """
        INSERT INTO long_term_memory
        (fact, category)
        VALUES (?, ?)
        """,
        (fact, category),
    )

    conn.commit()


def get_all_facts(conn):

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT fact, category, created_at
        FROM long_term_memory
        ORDER BY created_at ASC
        """
    )

    return cursor.fetchall()


def log_turn(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    item_id: Optional[str] = None,
):

    content = content.strip()

    if not content:
        return

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO conversations
        (
            session_id,
            item_id,
            role,
            content
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            session_id,
            item_id,
            role,
            content,
        ),
    )

    conn.commit()


def get_recent_conversation_text(
    conn,
    limit: int = 20,
):

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT role, content
        FROM conversations
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )

    rows = cursor.fetchall()

    rows.reverse()

    return "\n".join(
        f"{role}: {content}"
        for role, content in rows
    )

# ========================================================
# REMINDERS
# ========================================================

def _ensure_reminder_columns(conn):
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(reminders)")
    columns = {row[1] for row in cursor.fetchall()}
    wanted = {
        "reminder_id": "TEXT",
        "title": "TEXT",
        "scheduled_at": "TEXT",
        "priority": "TEXT DEFAULT 'normal'",
        "notification_channel": "TEXT DEFAULT 'shadow'",
        "status": "TEXT DEFAULT 'active'",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
        "delivered_at": "TEXT",
    }
    for name, spec in wanted.items():
        if name not in columns:
            cursor.execute(f"ALTER TABLE reminders ADD COLUMN {name} {spec}")


def _init_reminder_tables(conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reminder_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal',
            notification_channel TEXT NOT NULL DEFAULT 'shadow',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            delivered_at TEXT
        )
        """
    )
    _ensure_reminder_columns(conn)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, scheduled_at)"
    )
    conn.commit()


def create_reminder_record(conn, reminder_id, title, scheduled_at, priority='normal', notification_channel='shadow'):
    _init_reminder_tables(conn)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO reminders
        (reminder_id, title, scheduled_at, priority, notification_channel, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        """,
        (reminder_id, title.strip(), scheduled_at, priority, notification_channel),
    )
    conn.commit()
    return {
        "success": True,
        "reminder_id": reminder_id,
        "title": title.strip(),
        "status": "active",
    }


def cancel_reminder_record(conn, reminder_id):
    _init_reminder_tables(conn)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT title, status FROM reminders WHERE reminder_id = ? LIMIT 1",
        (reminder_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {"success": False, "error": "Reminder not found.", "reminder_id": reminder_id}
    if row[1] == "cancelled":
        return {"success": True, "cancelled": True, "reminder_id": reminder_id, "title": row[0], "message": "Reminder was already cancelled."}
    cursor.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE reminder_id = ? AND status = 'active'",
        (reminder_id,),
    )
    conn.commit()
    return {"success": True, "cancelled": True, "reminder_id": reminder_id, "title": row[0], "message": "Reminder cancelled."}


def get_due_reminders(conn):
    _init_reminder_tables(conn)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, reminder_id, title, scheduled_at, priority, notification_channel, status
        FROM reminders
        WHERE status = 'active'
          AND scheduled_at <= strftime('%Y-%m-%d %H:%M:%S', 'now')
        ORDER BY scheduled_at ASC
        LIMIT 20
        """
    )
    return cursor.fetchall()


def mark_reminder_delivered(conn, reminder_id):
    _init_reminder_tables(conn)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE reminders SET status = 'delivered', delivered_at = CURRENT_TIMESTAMP WHERE reminder_id = ? AND status = 'active'",
        (reminder_id,),
    )
    conn.commit()


# ========================================================
# STUDY / POMODORO
# ========================================================

def _init_study_tables(conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            topic TEXT NOT NULL,
            focus_minutes INTEGER NOT NULL,
            break_minutes INTEGER NOT NULL,
            cycles_target INTEGER NOT NULL,
            current_cycle INTEGER NOT NULL DEFAULT 1,
            phase TEXT NOT NULL DEFAULT 'focus',
            status TEXT NOT NULL DEFAULT 'active',
            phase_started_at TEXT,
            phase_end_at TEXT,
            paused_remaining_seconds INTEGER,
            total_focus_seconds INTEGER NOT NULL DEFAULT 0,
            total_break_seconds INTEGER NOT NULL DEFAULT 0,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_study_status ON study_sessions(status)"
    )
    conn.commit()


def create_study_session(conn, session_id, topic, focus_minutes, break_minutes, cycles_target, phase_started_at, phase_end_at):
    _init_study_tables(conn)
    cursor = conn.cursor()
    # Only one active/paused study session at a time.
    cursor.execute(
        "UPDATE study_sessions SET status='stopped' WHERE status IN ('active','paused')"
    )
    cursor.execute(
        """
        INSERT INTO study_sessions
        (session_id, topic, focus_minutes, break_minutes, cycles_target, current_cycle, phase, status, phase_started_at, phase_end_at)
        VALUES (?, ?, ?, ?, ?, 1, 'focus', 'active', ?, ?)
        """,
        (session_id, topic.strip(), focus_minutes, break_minutes, cycles_target, phase_started_at, phase_end_at),
    )
    conn.commit()
    return get_study_session(conn, session_id)


def get_study_session(conn, session_id=None):
    _init_study_tables(conn)
    cursor = conn.cursor()
    if session_id:
        cursor.execute("SELECT * FROM study_sessions WHERE session_id = ? LIMIT 1", (session_id,))
    else:
        cursor.execute("SELECT * FROM study_sessions WHERE status IN ('active','paused') ORDER BY id DESC LIMIT 1")
    return cursor.fetchone()


def update_study_session(conn, session_id, **fields):
    _init_study_tables(conn)
    allowed = {
        'current_cycle','phase','status','phase_started_at','phase_end_at',
        'paused_remaining_seconds','total_focus_seconds','total_break_seconds','completed_at'
    }
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ', '.join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [session_id]
    conn.execute(f"UPDATE study_sessions SET {assignments} WHERE session_id = ?", values)
    conn.commit()


def get_study_summary(conn, days=1):
    _init_study_tables(conn)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COALESCE(SUM(total_focus_seconds), 0),
               COUNT(*),
               COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0)
        FROM study_sessions
        WHERE started_at >= datetime('now', ?)
        """,
        (f'-{int(days)} day',),
    )
    focus_seconds, sessions, completed = cursor.fetchone()
    return {
        'success': True,
        'days': days,
        'focus_seconds': int(focus_seconds or 0),
        'focus_minutes': int((focus_seconds or 0) // 60),
        'sessions': int(sessions or 0),
        'completed_sessions': int(completed or 0),
    }
