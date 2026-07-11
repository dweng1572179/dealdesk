"""Deals — the CRM core: pipeline board, deal detail, inline edit, tasks. Moving a
deal between stages is a per-card <select> that HTMX-posts (no drag-drop JS lib)."""
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db
from .app import app, base_ctx, require_auth, templates
from .models import DEAL_TYPES, PIPELINES, PROPERTY_TYPES, default_stage

_INT = {"purchase_price", "loan_amount", "noi"}
_FLOAT = {"ltv", "interest_rate", "dscr", "cap_rate"}


def _coerce(form) -> dict:
    """Form values -> typed deal fields. Blank numeric -> None (don't store 0)."""
    out: dict = {}
    for k in ("name", "pipeline", "stage", "deal_type", "property_type", "address",
              "city", "state", "sponsor", "lender_name", "status", "notes"):
        v = (form.get(k) or "").strip()
        if v:
            out[k] = v
    for k in _INT | _FLOAT:
        v = (form.get(k) or "").strip().replace(",", "").replace("$", "").replace("%", "")
        if v:
            try:
                out[k] = int(float(v)) if k in _INT else float(v)
            except ValueError:
                pass
    return out


def _board_ctx(request: Request, pipeline: str) -> dict:
    pipeline = pipeline if pipeline in PIPELINES else "acquisition"
    deals = db.list_deals(pipeline)
    columns = [{"stage": s, "deals": [d for d in deals if d["stage"] == s]}
               for s in PIPELINES[pipeline]]
    ctx = base_ctx(request)
    ctx |= {"pipeline": pipeline, "pipelines": list(PIPELINES), "columns": columns,
            "property_types": PROPERTY_TYPES, "deal_types": DEAL_TYPES}
    return ctx


@app.get("/deals", response_class=HTMLResponse)
def deals_board(request: Request, pipeline: str = "acquisition", _=Depends(require_auth)):
    return templates.TemplateResponse("deals.html", _board_ctx(request, pipeline))


@app.post("/deals", response_class=HTMLResponse)
async def deals_create(request: Request, _=Depends(require_auth)):
    form = await request.form()
    fields = _coerce(form)
    fields.setdefault("pipeline", "acquisition")
    fields["stage"] = default_stage(fields["pipeline"])
    deal_id = db.create_deal(fields)
    db.add_activity("system", f"Created deal {fields.get('name','Untitled')}", deal_id)
    return RedirectResponse(f"/deal/{deal_id}", status_code=303)


def _deal_ctx(request: Request, deal_id: int) -> dict | None:
    deal = db.get_deal(deal_id)
    if not deal:
        return None
    ctx = base_ctx(request)
    ctx |= {
        "deal": deal,
        "tasks": db.list_tasks(deal_id),
        "documents": db.list_documents(deal_id),
        "contacts": db.list_contacts(deal_id),
        "activity": db.list_activity(limit=20, deal_id=deal_id),
        "stages": PIPELINES.get(deal["pipeline"], PIPELINES["acquisition"]),
        "property_types": PROPERTY_TYPES, "deal_types": DEAL_TYPES,
    }
    return ctx


@app.get("/deal/{deal_id}", response_class=HTMLResponse)
def deal_detail(request: Request, deal_id: int, _=Depends(require_auth)):
    ctx = _deal_ctx(request, deal_id)
    if ctx is None:
        return RedirectResponse("/deals", status_code=303)
    return templates.TemplateResponse("deal.html", ctx)


@app.post("/deal/{deal_id}", response_class=HTMLResponse)
async def deal_update(request: Request, deal_id: int, _=Depends(require_auth)):
    form = await request.form()
    db.update_deal(deal_id, _coerce(form))
    return RedirectResponse(f"/deal/{deal_id}", status_code=303)


@app.post("/deal/{deal_id}/stage", response_class=HTMLResponse)
def deal_move(request: Request, deal_id: int, stage: str = Form(...),
              pipeline: str = Form("acquisition"), _=Depends(require_auth)):
    db.move_deal_stage(deal_id, stage)
    deal = db.get_deal(deal_id)
    db.add_activity("stage", f"{deal['name']} → {stage}", deal_id)
    # re-render the whole board so the card lands in its new column
    return templates.TemplateResponse("_board.html", _board_ctx(request, pipeline))


@app.post("/deal/{deal_id}/delete")
def deal_delete(deal_id: int, _=Depends(require_auth)):
    db.delete_deal(deal_id)
    return RedirectResponse("/deals", status_code=303)


# --- tasks (HTMX partial) ----------------------------------------------------

def _tasks(request: Request, deal_id: int) -> HTMLResponse:
    return templates.TemplateResponse(
        "_tasks.html", {"request": request, "deal_id": deal_id, "tasks": db.list_tasks(deal_id)})


@app.post("/deal/{deal_id}/task", response_class=HTMLResponse)
def task_add(request: Request, deal_id: int, body: str = Form(...),
             due: str = Form(""), _=Depends(require_auth)):
    body = body.strip()[:500]
    if body:
        db.add_task(deal_id, body, due.strip() or None)
    return _tasks(request, deal_id)


@app.post("/task/{task_id}/toggle", response_class=HTMLResponse)
def task_toggle(request: Request, task_id: int, done: str = Form("1"), _=Depends(require_auth)):
    deal_id = db.set_task_done(task_id, done == "1")
    return _tasks(request, deal_id or 0)


@app.delete("/task/{task_id}", response_class=HTMLResponse)
def task_delete(request: Request, task_id: int, _=Depends(require_auth)):
    deal_id = db.delete_task(task_id)
    return _tasks(request, deal_id or 0)
