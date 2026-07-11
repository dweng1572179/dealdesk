"""Settings from .env. One source of truth. The /settings page writes DB `setting`
rows that OVERRIDE these live (settings_store.py) — mirrors OpenProp. So editing
.env alone does nothing if the DB already holds that key; change it on /settings."""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    dealdesk_password: str = "changeme"
    secret_key: str = ""
    session_https_only: bool = False  # set true behind HTTPS (adds Secure to the cookie)

    # --- AI (the whole product runs on one BYO Anthropic key; rules fallback without) ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-4-8"  # or claude-haiku-4-5 for cheaper/faster

    # --- email loop (stdlib IMAP/SMTP — BYO inbox; Gmail needs an App Password) ---
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    email_user: str = ""      # full address, e.g. you@gmail.com
    email_password: str = ""  # App Password, NOT your account password
    email_from: str = ""      # defaults to email_user if blank

    monthly_budget_cents: int = 2500  # hard cap on AI spend per calendar month (real token cost)
    db_path: str = "dealdesk.db"

    @field_validator("*", mode="before")
    @classmethod
    def _drop_inline_comment(cls, v, info):
        """`KEY=   # note` in .env yields the comment as the value — python-dotenv only
        strips an inline comment when the value is non-empty. Left alone, a blank
        SECRET_KEY reads as truthy and app.py never generates a random one."""
        if isinstance(v, str) and v.lstrip().startswith("#"):
            return cls.model_fields[info.field_name].default
        return v


settings = Settings()


if __name__ == "__main__":  # python -m app.config
    import os
    os.environ |= {"SECRET_KEY": "  # leave blank -> generated", "MONTHLY_BUDGET_CENTS": "250"}
    s = Settings(_env_file=None)
    assert s.secret_key == "", f"comment leaked into secret_key: {s.secret_key!r}"
    assert s.monthly_budget_cents == 250, s.monthly_budget_cents
    assert Settings(_env_file=None, anthropic_api_key="sk-abc").anthropic_api_key == "sk-abc"
    print("config ok — inline comments dropped, real values preserved")
