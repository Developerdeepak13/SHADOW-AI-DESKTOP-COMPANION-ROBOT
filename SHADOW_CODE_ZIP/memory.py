import json

from database import get_all_facts, save_fact
from llm_utils import call_gemini


def load_long_term_memory_summary(conn):
    facts = get_all_facts(conn)

    if not facts:
        return "No long-term memory is available yet."

    grouped = {}
    for fact, category, created_at in facts:
        grouped.setdefault(category, []).append(fact)

    lines = []
    for category, category_facts in grouped.items():
        lines.append(f"{category.upper()}:")
        for fact in category_facts:
            lines.append(f"- {fact}")

    return "\n".join(lines)


def extract_facts_from_conversation(conversation_text: str):
    if not conversation_text.strip():
        return []

    prompt = f"""
Extract durable facts about the user from this conversation.

Only extract facts that are useful across future sessions.

Allowed categories:

identity
preference
topic_strength
topic_weakness
goal
other

Do NOT extract:

- temporary statements
- jokes
- random questions
- facts about other people
- fictional information
- information that is unlikely to matter later

Return ONLY valid JSON.

Format:

[
  {{
    "fact": "The user prefers Python for DSA.",
    "category": "preference"
  }}
]

Conversation:

{conversation_text}
"""

    try:
        result = call_gemini(
            prompt,
            instructions=(
                "You are Shadow's memory extraction system. "
                "Extract only durable user facts. "
                "Return strict JSON only. "
                "Do not include markdown or explanations."
            ),
        )

    except Exception as e:
        error_text = str(e)

        if (
            "credit_balance_exhausted" in error_text
            or "insufficient_quota" in error_text.lower()
            or "quota exceeded" in error_text.lower()
        ):
            print(
                "⚠️ Memory consolidation skipped: "
                "Gemini API quota/credits are exhausted."
            )
        else:
            print(f"⚠️ Gemini memory extraction failed: {e}")

        return []

    try:
        data = json.loads(result)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    try:
        start = result.find("[")
        end = result.rfind("]")
        if start != -1 and end != -1:
            data = json.loads(result[start:end + 1])
            if isinstance(data, list):
                return data
    except Exception:
        pass

    print("⚠️ Gemini returned invalid memory JSON.")
    return []


def consolidate_session_memory(conn, conversation_text: str):
    facts = extract_facts_from_conversation(conversation_text)
    saved_count = 0

    for item in facts:
        if not isinstance(item, dict):
            continue

        fact = item.get("fact")
        category = item.get("category")

        if not fact or not category:
            continue

        try:
            save_fact(conn, fact, category)
            saved_count += 1
        except Exception as e:
            print(f"⚠️ Failed to save memory: {e}")

    print(
        f"🧠 Memory consolidation complete. "
        f"Saved {saved_count} durable fact(s)."
    )
