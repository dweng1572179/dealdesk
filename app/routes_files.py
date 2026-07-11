"""Files — upload a term sheet / OM / email to a deal, extract text, pull structured
terms (ai.extract_terms), and optionally apply them to the deal. This is Lev's
"connect a document → terms extracted (~95%)" loop, BYO-key."""
from fastapi import Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from . import ai, db, docparse
from .app import app, require_auth, templates
from .budget import BudgetExceeded


@app.post("/deal/{deal_id}/upload", response_class=HTMLResponse)
async def deal_upload(request: Request, deal_id: int, file: UploadFile,
                      apply: str = Form(""), _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    text = docparse.extract_text(file.filename, await file.read())
    try:
        terms = ai.extract_terms(text)
    except BudgetExceeded as e:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": str(e)})
    db.save_document(deal_id, file.filename, text, terms)
    summary = terms.get("summary", "terms extracted")
    db.add_activity("extract", f"{file.filename}: {summary}", deal_id)

    applied = {}
    if apply == "1":
        applied = {k: v for k, v in terms.items() if k != "summary"}
        # the extracted "property_name" maps onto the deal's `name` column
        if "property_name" in applied:
            applied["name"] = applied.pop("property_name")
        if applied:
            db.update_deal(deal_id, applied)
            db.add_activity("system",
                            f"Applied {len(applied)} extracted term(s) to {deal['name']}", deal_id)
    return templates.TemplateResponse("_terms.html", {
        "request": request, "deal_id": deal_id, "filename": file.filename,
        "terms": terms, "applied": applied, "documents": db.list_documents(deal_id)})
