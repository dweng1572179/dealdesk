"""Settings — paste your Anthropic key + connect an inbox in the browser; saved to
the DB and applied live (no restart). Secrets are never rendered back; a blank
secret field keeps the stored value."""
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from . import ai, budget, inbox, settings_store
from .app import app, base_ctx, require_auth, templates
from .config import settings
from .settings_store import FIELDS


def _fields_ctx() -> list[dict]:
    out = []
    for name, label, kind in FIELDS:
        cur = getattr(settings, name, "")
        f = {"name": name, "label": label, "kind": kind, "secret": kind == "secret"}
        if kind == "secret":
            f["is_set"] = bool(cur)
        else:
            f["value"] = cur
        out.append(f)
    return out


def _status() -> list[dict]:
    return [
        {"label": "AI agent · term extraction · drafting", "on": ai.available(),
         "note": "needs ANTHROPIC_API_KEY (rules/template fallback otherwise)"},
        {"label": "Email loop (sync inbox · send)", "on": inbox.configured(),
         "note": "needs email address + App Password"},
        {"label": "Lender matching", "on": True, "note": "local — always on"},
    ]


def _ctx(request: Request, saved: bool = False) -> dict:
    ctx = base_ctx(request)
    ctx |= {"fields": _fields_ctx(), "status": _status(), "saved": saved,
            "remaining_cents": budget.remaining_cents()}
    return ctx


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("settings.html", _ctx(request))


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, _=Depends(require_auth)):
    form = await request.form()
    updates: dict[str, str] = {}
    for name, _label, kind in FIELDS:
        v = (form.get(name) or "").strip()
        # blank secret = keep stored; blank text/int = keep too (don't wipe host/port).
        if v:
            updates[name] = v
    settings_store.save(updates)
    return templates.TemplateResponse("settings.html", _ctx(request, saved=True))


@app.post("/settings/test/anthropic", response_class=HTMLResponse)
def test_anthropic(request: Request, _=Depends(require_auth)):
    """Cheapest possible live call (1 token) to prove the key + model actually work.
    Bills a token against your Anthropic account, not the local budget meter — a
    connection test shouldn't consume the monthly cap."""
    if not ai.available():
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": "No Anthropic key set — paste one above and Save first."})
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=30.0)
        resp = client.messages.create(model=settings.llm_model, max_tokens=1,
                                       messages=[{"role": "user", "content": "hi"}])
        model = getattr(resp, "model", settings.llm_model)
        return templates.TemplateResponse("_flash.html", {
            "request": request, "msg": f"Anthropic key works — reached {model}."})
    except Exception as e:  # noqa: BLE001 — surface the real API error to the user
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": f"Anthropic test failed: {type(e).__name__}: {e}"})


@app.post("/settings/test/email", response_class=HTMLResponse)
def test_email(request: Request, _=Depends(require_auth)):
    """Log in to IMAP and SMTP (no message sent) to prove the inbox credentials +
    host/port actually connect."""
    if not inbox.configured():
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": "No inbox connected — add your address + App Password above and Save first."})
    try:
        inbox.test_connection()
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": f"Email test failed: {e}"})
    return templates.TemplateResponse("_flash.html", {
        "request": request, "msg": f"Inbox connected — IMAP {settings.imap_host} and SMTP {settings.smtp_host} both authenticated."})
