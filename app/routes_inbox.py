"""Inbox loop — sync recent email into the activity feed (matching senders to deal
contacts) and send email from a deal. Lev runs a dedicated email microservice with
OAuth; here it's stdlib IMAP/SMTP against an inbox you connect in Settings.

The activity feed is polled by HTMX (`/activity`), which is the open stand-in for
Lev's Pusher realtime push."""
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

    logged = 0
    for m in messages:
        # match the sender to a known contact so the reply lands on its deal (Lev's
        # "Steve from Cain replied" → attached to the right deal).
        contact = db.contact_by_email(m["from_email"])
        deal_id = contact["deal_id"] if contact and contact.get("deal_id") else None
        who = (contact["name"] if contact else m["from"]) or m["from_email"]
        db.add_activity("email_in", f"{who} replied · {m['subject']}", deal_id)
        logged += 1
    db.add_activity("system", f"Inbox sync: {logged} message(s) pulled")
    return templates.TemplateResponse(
        "_activity.html", {"request": request, "activity": db.list_activity(limit=15)})


@app.post("/deal/{deal_id}/email/send", response_class=HTMLResponse)
def email_send(request: Request, deal_id: int, to: str = Form(...), subject: str = Form(...),
               body: str = Form(...), _=Depends(require_auth)):
    if not inbox.configured():
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": "Connect an inbox first — Settings → email address + App Password."})
    try:
        inbox.send(to.strip(), subject.strip(), body)
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse("_error.html", {"request": request, "msg": f"Send failed: {e}"})
    db.add_activity("email_out", f"Emailed {to.strip()} · {subject.strip()}", deal_id)
    return templates.TemplateResponse("_flash.html", {"request": request, "msg": f"Sent to {to.strip()}."})
