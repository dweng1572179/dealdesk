"""Placements — which lenders a deal was shopped to and where each stands (Lev's
"Placements" deal column). Persisted from the match view ("shop this deal to X") and
editable on the deal page. All HTMX partials targeting #placements."""
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import db
from .app import app, require_auth, templates
from .models import PLACEMENT_STATUSES


def _num(v):
    v = (v or "").strip().replace(",", "").replace("$", "").replace("%", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _placements(request: Request, deal_id: int) -> HTMLResponse:
    return templates.TemplateResponse("_placements.html", {
        "request": request, "deal_id": deal_id,
        "placements": db.list_placements(deal_id), "statuses": PLACEMENT_STATUSES})


@app.post("/deal/{deal_id}/placement", response_class=HTMLResponse)
async def placement_add(request: Request, deal_id: int, _=Depends(require_auth)):
    """Add or update a placement. From the match view this carries a lender_id +
    prefilled terms; from the deal page it's a free-typed lender name."""
    if not db.get_deal(deal_id):
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    form = await request.form()
    name = (form.get("lender_name") or "").strip()
    if not name:
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": "A placement needs a lender name."})
    # No status field on the hand-add / "+ shop" forms → pass None so an existing
    # placement keeps its status (db defaults a brand-new one to 'shopped'). An
    # explicit invalid status falls back to 'shopped'.
    status = (form.get("status") or "").strip()
    if status and status not in PLACEMENT_STATUSES:
        status = "shopped"
    lender_id = form.get("lender_id")
    db.upsert_placement({
        "deal_id": deal_id, "lender_name": name,
        "lender_id": int(lender_id) if (lender_id or "").isdigit() else None,
        "status": status or None,
        "loan_amount": (lambda v: int(v) if v is not None else None)(_num(form.get("loan_amount"))),
        "rate": _num(form.get("rate")), "ltv": _num(form.get("ltv")),
        "term_years": _num(form.get("term_years")), "amort_years": _num(form.get("amort_years")),
        "notes": (form.get("notes") or "").strip() or None})
    db.add_activity("note", f"Placement: {name} · {status or 'shopped'}", deal_id)
    return _placements(request, deal_id)


@app.post("/placement/{placement_id}/status", response_class=HTMLResponse)
def placement_status(request: Request, placement_id: int, status: str = Form(...),
                     _=Depends(require_auth)):
    p = db.get_placement(placement_id)
    if not p:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown placement."})
    if status in PLACEMENT_STATUSES:
        db.upsert_placement({"deal_id": p["deal_id"], "lender_name": p["lender_name"], "status": status})
        db.add_activity("note", f"Placement {p['lender_name']} → {status}", p["deal_id"])
    return _placements(request, p["deal_id"])


@app.delete("/placement/{placement_id}", response_class=HTMLResponse)
def placement_delete(request: Request, placement_id: int, _=Depends(require_auth)):
    deal_id = db.delete_placement(placement_id)
    if deal_id is None:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown placement."})
    return _placements(request, deal_id)
