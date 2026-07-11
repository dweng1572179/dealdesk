"""The AI Agent — Lev's centerpiece. Chat over your deals, draft emails, generate a
deal memo. Everything degrades to a rules/template fallback with no Anthropic key
(ai.py), so these routes never hard-require a key."""
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import ai, db
from .app import app, require_auth, templates


@app.post("/agent", response_class=HTMLResponse)
def agent_ask(request: Request, question: str = Form(...), _=Depends(require_auth)):
    question = question.strip()
    if not question:
        return templates.TemplateResponse(
            "_error.html", {"request": request, "msg": "Ask something about your deals."})
    answer = ai.agent_reply(question, db.deals_context())
    db.add_activity("agent", f"Q: {question[:120]}")
    return templates.TemplateResponse(
        "_agent_reply.html", {"request": request, "question": question, "answer": answer})


@app.post("/deal/{deal_id}/draft", response_class=HTMLResponse)
def deal_draft(request: Request, deal_id: int, intent: str = Form("Follow up on the deal."),
               to_name: str = Form(""), _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    body = ai.draft_email(deal, intent.strip() or "Follow up on the deal.", to_name.strip())
    subject, _, rest = body.partition("\n")
    subject = subject.replace("Subject:", "").strip() if subject.lower().startswith("subject:") else f"{deal['name']} — follow up"
    return templates.TemplateResponse("_draft.html", {
        "request": request, "deal_id": deal_id, "subject": subject,
        "body": rest.strip() or body, "contacts": db.list_contacts(deal_id)})


@app.post("/deal/{deal_id}/memo", response_class=HTMLResponse)
def deal_memo(request: Request, deal_id: int, _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    md = ai.deal_memo(deal, db.list_contacts(deal_id), db.list_documents(deal_id))
    db.add_activity("agent", f"Generated deal memo for {deal['name']}", deal_id)
    return templates.TemplateResponse(
        "_memo.html", {"request": request, "deal": deal, "markdown": md})
