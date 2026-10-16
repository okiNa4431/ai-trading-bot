"""Never retain upstream exception text or an echoed credential."""

import os
import re


def redact(text: str) -> str:
    for name in (
        "GEMINI_API_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_DB_URL",
        "SUPABASE_ACCESS_TOKEN",
    ):
        key = os.environ.get(name)
        if key:
            text = text.replace(key, "[REDACTED]")
    return re.sub(
        r"AIza[0-9A-Za-z_-]{20,}|sb_secret_[0-9A-Za-z_-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+",
        "[REDACTED]",
        text,
    )
