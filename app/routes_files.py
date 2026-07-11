"""Files — upload a term sheet / OM / email to a deal, extract text, pull structured
terms (ai.extract_terms), and optionally apply them to the deal. This is Lev's
"connect a document → terms extracted (~95%)" loop, BYO-key. The vault keeps the
original bytes so a document can be viewed or downloaded, not just re-read as text."""
import mimetypes

from fastapi import Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from . import ai, db, docparse
from .app import app, require_auth, templates
from .budget import BudgetExceeded

# how large an uploaded file we accept (bytes). ponytail: a flat ceiling, not
# streaming-to-disk — the DB is one file and blobs live in it; a term sheet is a few MB.
MAX_UPLOAD = 25 * 1024 * 1024

# Content types safe to render inline in the browser; everything else downloads as an
# attachment so an uploaded .html can't run as a page in the app's own origin.
_INLINE_MIME = {"application/pdf", "text/plain", "image/png", "image/jpeg", "image/gif",
                "image/webp"}


@app.post("/deal/{deal_id}/upload", response_class=HTMLResponse)
async def deal_upload(request: Request, deal_id: int, file: UploadFile,
                      apply: str = Form(""), _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        return templates.TemplateResponse("_error.html", {
            "request": request,
            "msg": f"File is {len(raw) // 1024 // 1024} MB; the limit is {MAX_UPLOAD // 1024 // 1024} MB."})
    text = docparse.extract_text(file.filename, raw)
    try:
        terms = ai.extract_terms(text)
    except BudgetExceeded as e:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": str(e)})
    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    db.save_document(deal_id, file.filename, text, terms, data=raw, mime=mime)
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


def _content_disposition(mime: str, filename: str) -> str:
    """Inline for browser-safe types, attachment otherwise. RFC 5987 filename* so a
    name with spaces/unicode survives the header."""
    from urllib.parse import quote
    kind = "inline" if mime in _INLINE_MIME else "attachment"
    ascii_name = (filename or "document").encode("ascii", "ignore").decode() or "document"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename or 'document')}"


@app.get("/document/{doc_id}")
def document_serve(doc_id: int, _=Depends(require_auth)):
    """Serve a stored document's original bytes back (view inline or download)."""
    doc = db.get_document(doc_id)
    if not doc:
        return Response("Unknown document.", status_code=404)
    if doc.get("data") is None:
        # a document saved before the vault existed (v1) has no stored bytes
        return Response("This document was saved before file storage — re-upload it to view.",
                        status_code=404)
    mime = doc.get("mime") or "application/octet-stream"
    return Response(
        content=doc["data"], media_type=mime,
        headers={"Content-Disposition": _content_disposition(mime, doc["filename"])})


@app.delete("/document/{doc_id}", response_class=HTMLResponse)
def document_delete(request: Request, doc_id: int, _=Depends(require_auth)):
    deal_id = db.delete_document(doc_id)
    if deal_id is None:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown document."})
    return templates.TemplateResponse("_documents.html", {
        "request": request, "deal_id": deal_id, "documents": db.list_documents(deal_id)})
