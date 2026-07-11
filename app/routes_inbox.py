"""Inbox loop — sync recent email into per-deal threads + the activity feed, compose /
reply / template, and send. Instead of a dedicated email microservice with OAuth, here
it's stdlib IMAP/SMTP against an inbox you connect in Settings, with synced + sent mail
stored per deal (db.email) so each deal has a real thread.

The activity feed is polled by HTMX (`/activity`), in place of a websocket/push layer."""
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import db, inbox
from .app import app, require_auth, templates


@app.get("/activity", response_class=HTMLResponse)
def activity_feed(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(
        "_activity.html", {"request": request, "activity": db.list_activity(limit=15)})


@app.post("/inbox/sync", response_class=HTMLResponse)
def inbox_sync(request: Request, _=Depends(require_auth)):
    if not inbox.configured():
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": "Connect an inbox first — Settings → email address + App Password."})
    try:
        messages = inbox.fetch(limit=15)
    except Exception as e:  # noqa: BLE001 — surface IMAP/login errors to the user, don't 500
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": f"Inbox sync failed: {e}"})

    deals = db.list_deals()
    logged = 0
    for m in messages:
        # Was this message already synced? Check BEFORE saving — save_email is idempotent
        # (returns the existing id on a Message-ID conflict), so a re-sync must skip the
        # re-log here, not rely on the return value. ponytail: a message with no
        # Message-ID has no dedup key and will re-log on each sync — rare (real mail sets
        # one); dedup by content hash only if a provider omits it.
        mid = m.get("message_id")
        already = bool(mid) and db.email_exists(mid)
        # match the sender to a deal (its contact, else the deal name in the subject) so
        # the message lands on the right thread ("Steve from Cain replied" → the right deal).
        contact = db.contact_by_email(m["from_email"])
        deal_id = inbox.match_email_to_deal(m["from_email"], m["subject"], contact, deals)
        db.save_email("in", m["from_email"], None, m["subject"], m.get("body"),
                      deal_id=deal_id, message_id=mid)
        if already:
            continue   # stored idempotently already — don't re-log to the feed
        who = (contact["name"] if contact else m["from"]) or m["from_email"]
        db.add_activity("email_in", f"{who} · {m['subject']}", deal_id)
        logged += 1
    db.add_activity("system", f"Inbox sync: {logged} new message(s) pulled")
    return templates.TemplateResponse(
        "_activity.html", {"request": request, "activity": db.list_activity(limit=15)})


# --- per-deal email thread + compose -----------------------------------------

def _thread(request: Request, deal_id: int, flash: str = "") -> HTMLResponse:
    return templates.TemplateResponse("_emails.html", {
        "request": request, "deal_id": deal_id, "emails": db.list_emails(deal_id),
        "email_templates": list(inbox.FOLLOWUP_TEMPLATES), "email_on": inbox.configured(),
        "flash": flash or None, "prefill": None})


@app.get("/deal/{deal_id}/emails", response_class=HTMLResponse)
def deal_emails(request: Request, deal_id: int, _=Depends(require_auth)):
    if not db.get_deal(deal_id):
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    return _thread(request, deal_id)


@app.get("/deal/{deal_id}/email/compose", response_class=HTMLResponse)
def email_compose(request: Request, deal_id: int, reply_to: str = "", template: str = "",
                  _=Depends(require_auth)):
    """Open the compose box, optionally pre-filled from a template or a reply to a stored
    inbound email (To = sender, quoted original body)."""
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    prefill = {"to": "", "subject": "", "body": ""}
    if reply_to.isdigit():
        e = db.get_email(int(reply_to))
        if e and e["deal_id"] == deal_id:
            subj = e["subject"] or ""
            prefill = {"to": e["from_addr"] or "",
                       "subject": subj if subj.lower().startswith("re:") else f"Re: {subj}",
                       "body": "\n\n\n> " + "\n> ".join((e["body"] or "").splitlines()[:20])}
    elif template:
        filled = inbox.fill_template(template, deal)
        if filled:
            prefill = {"to": "", **filled}
    return templates.TemplateResponse("_compose.html", {
        "request": request, "deal_id": deal_id, "prefill": prefill,
        "email_templates": list(inbox.FOLLOWUP_TEMPLATES), "email_on": inbox.configured()})


@app.post("/deal/{deal_id}/email/send", response_class=HTMLResponse)
def email_send(request: Request, deal_id: int, to: str = Form(...), subject: str = Form(...),
               body: str = Form(...), _=Depends(require_auth)):
    if not db.get_deal(deal_id):
        # check BEFORE sending — otherwise a bad deal_id delivers the mail, then storage
        # would attach it to a nonexistent deal.
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    if not inbox.configured():
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": "Connect an inbox first — Settings → email address + App Password."})
    try:
        inbox.send(to.strip(), subject.strip(), body)
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse("_error.html", {"request": request, "msg": f"Send failed: {e}"})
    db.save_email("out", None, to.strip(), subject.strip(), body, deal_id=deal_id)
    db.add_activity("email_out", f"Emailed {to.strip()} · {subject.strip()}", deal_id)
    return _thread(request, deal_id, flash=f"Sent to {to.strip()}.")
