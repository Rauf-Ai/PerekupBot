"""Entitlements and validation for user-added Telegram sources."""

import hashlib
import re


CUSTOM_SOURCE_LIMITS = {"pro": 3, "business": 10, "team": 20}


def custom_source_limit(plan: str | None, *, is_admin: bool = False) -> int:
    return 20 if is_admin else CUSTOM_SOURCE_LIMITS.get(plan or "", 0)


def parse_telegram_source(value: str) -> tuple[str, str] | None:
    """Return (identifier, display name) for a public username or invite link."""
    raw = value.strip()
    if raw.startswith("@"):
        name = raw[1:]
    else:
        invite = re.fullmatch(r"(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]{8,})/?", raw, re.I)
        if invite:
            return f"invite:{invite.group(1)}", "Закрытый чат"
        public = re.fullmatch(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z0-9_]{5,32})/?", raw, re.I)
        if not public:
            return None
        name = public.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", name):
        return None
    return name, f"@{name}"


def custom_source_key(identifier: str) -> str:
    return "custom_tg_" + hashlib.sha256(identifier.casefold().encode()).hexdigest()[:24]
