"""Runtime settings — paste keys in the browser instead of editing .env + restarting.
DB `setting` rows override the .env-loaded `settings` object live. AI + email read
`settings` fresh on each call, so there's no provider cache to rebuild (unlike
OpenProp) — saving just re-applies the overrides. Precedence: DB > .env > default."""
from .config import settings
from .db import get_conn

# (name, label, kind) — kind: "secret" | "text" | "int" | "select:a,b,c"
FIELDS = [
    ("anthropic_api_key", "Anthropic API key (the AI agent + extraction + drafting)", "secret"),
    ("llm_model", "LLM model", "text"),
    ("email_user", "Email address (for the inbox loop)", "text"),
    ("email_password", "Email App Password (NOT your login password)", "secret"),
    ("email_from", "From address (blank = same as email address)", "text"),
    ("imap_host", "IMAP host", "text"),
    ("imap_port", "IMAP port", "int"),
    ("smtp_host", "SMTP host", "text"),
    ("smtp_port", "SMTP port", "int"),
    ("monthly_budget_cents", "Monthly AI-spend cap (cents)", "int"),
]
_KINDS = {name: kind for name, label, kind in FIELDS}


def _apply(name: str, value: str) -> None:
    if not hasattr(settings, name):
        return
    if _KINDS.get(name) == "int":
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
    setattr(settings, name, value)


def load_overrides() -> None:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM setting").fetchall()
    for r in rows:
        _apply(r["key"], r["value"])


def save(updates: dict[str, str]) -> None:
    with get_conn() as conn:
        for name, value in updates.items():
            conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (name, str(value)))
    for name, value in updates.items():
        _apply(name, str(value))


# --- internal flags (not user-facing config; the `setting` table doubles as a KV) ----
# ponytail: reuse the setting table for a couple of app flags (e.g. "seeded") rather
# than add a table. _apply() ignores keys that aren't real settings, so a flag can't
# clobber the config object.

def get_flag(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_flag(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
