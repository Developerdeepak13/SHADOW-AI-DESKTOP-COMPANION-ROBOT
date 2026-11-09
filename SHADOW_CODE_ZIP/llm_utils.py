import time
from google import genai
from config import GEMINI_API_KEY, GEMINI_MEMORY_MODEL

client = genai.Client(api_key=GEMINI_API_KEY)

def call_gemini(prompt, instructions="", max_retries=3):
    """Gemini background call used by memory consolidation."""
    last_error = None

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MEMORY_MODEL,
                contents=prompt,
                config={
                    "system_instruction": instructions,
                    "temperature": 0.2,
                },
            )
            return (response.text or "").strip()

        except Exception as e:
            last_error = e
            error_text = str(e)
            print(
                f"⚠️ Gemini request failed "
                f"(attempt {attempt + 1}/{max_retries}): {error_text}"
            )

            # Do not waste retries when the account has no quota/credits.
            quota_exhausted = (
                "credit_balance_exhausted" in error_text
                or "insufficient_quota" in error_text.lower()
                or "quota exceeded" in error_text.lower()
            )
            if quota_exhausted:
                break

            if (
                "503" in error_text
                or "UNAVAILABLE" in error_text
                or "429" in error_text
                or "RESOURCE_EXHAUSTED" in error_text
            ):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"⏳ Retrying Gemini in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue

            break

    raise last_error
