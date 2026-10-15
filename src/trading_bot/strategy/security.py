"""Never retain upstream exception text or an echoed credential."""

import os
import re


def redact(text: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", text)
