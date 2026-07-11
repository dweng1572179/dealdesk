"""Market — the capital-markets hub. Three things Lev sells as a proprietary data
moat, here running on data you own:
  • Lender matching   — rank your lender book against a deal (matching.py)
  • Underwriting model — build an Excel pro forma / DSCR / debt sizing (underwriting.py)
  • Market reference   — your Base rates + Recent-terms (closed-loan) comps, BYO/importable

Lev's real edge is a live feed of 7,000+ lenders / 16,000+ loan comps / 34 rate
benchmarks; the honest open trade is you maintain the reference data (seeded, then
edit/import your own)."""
import csv
import io
from io import BytesIO  # imported directly: the xlsx route has an `io` query param that shadows the module

from fastapi import Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from . import ai, db, matching, underwriting
from .app import app, base_ctx, require_auth, templates


# --- lender matching ---------------------------------------------------------

@app.get("/deal/{deal_id}/match", response_class=HTMLResponse)
def deal_match(request: Request, deal_id: int, _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    lenders = db.list_lenders()
    if not lenders:
        return templates.TemplateResponse("_error.html", {
            "request": request,
            "msg": "No lenders in your book yet — add them under CRM → Lenders (or import a CSV)."})
    ranked = matching.rank(deal, lenders)
    excluded = [r for r in matching.rank(deal, lenders, include_disqualified=True) if r["disqualified"]]
    return templates.TemplateResponse("_match.html", {
        "request": request, "deal": deal, "matches": ranked, "excluded": excluded})


@app.post("/deal/{deal_id}/match/{lender_id}/why", response_class=HTMLResponse)
def match_why(request: Request, deal_id: int, lender_id: int, _=Depends(require_auth)):
    deal, lender = db.get_deal(deal_id), db.get_lender(lender_id)
    if not deal or not lender:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal or lender."})
    text = ai.match_rationale(deal, lender) or "Add an Anthropic key in Settings for AI pros/cons."
    return templates.TemplateResponse(
        "_ai_text.html", {"request": request, "label": lender["name"], "text": text})


# --- underwriting model ------------------------------------------------------

def _assumptions(amort: int, growth: float, hold: int, io_flag: str) -> dict:
    return {"amort_years": max(1, amort), "noi_growth_pct": growth,
            "hold_years": min(max(1, hold), 15), "interest_only": io_flag == "1"}


@app.get("/deal/{deal_id}/underwrite", response_class=HTMLResponse)
def deal_underwrite(request: Request, deal_id: int, amort: int = 30, growth: float = 3.0,
                    hold: int = 5, io: str = "", _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    # No activity row here — the assumptions form re-hits this on every knob change
    # (hx-trigger="change"), which would flood the feed. The .xlsx export logs once.
    m = underwriting.compute(deal, **_assumptions(amort, growth, hold, io))
    return templates.TemplateResponse(
        "_underwrite.html", {"request": request, "deal": deal, "m": m})


@app.get("/deal/{deal_id}/model.xlsx")
def deal_model_xlsx(deal_id: int, amort: int = 30, growth: float = 3.0, hold: int = 5,
                    io: str = "", _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return HTMLResponse("Unknown deal.", status_code=404)
    m = underwriting.compute(deal, **_assumptions(amort, growth, hold, io))
    data = underwriting.workbook(deal, m)
    db.add_activity("agent", f"Exported underwriting model for {deal['name']}", deal_id)
    fname = "".join(c if c.isalnum() else "_" for c in deal["name"])[:60] or "model"
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}_underwriting.xlsx"'})


# --- market reference data ---------------------------------------------------

@app.get("/market", response_class=HTMLResponse)
def market(request: Request, imported: str = "", _=Depends(require_auth)):
    ctx = base_ctx(request)
    ctx |= {"base_rates": db.list_base_rates(), "comps": db.list_loan_comps(),
            "lender_count": len(db.list_lenders()), "imported": imported or None}
    return templates.TemplateResponse("market.html", ctx)


def _num(v):
    v = (v or "").strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(v)
    except ValueError:
        return None


@app.post("/market/rates/import")
async def rates_import(file: UploadFile, _=Depends(require_auth)):
    """CSV: name,value,delta_1d,delta_1m (upsert by name)."""
    reader = csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig", errors="replace")))
    n = 0
    for row in reader:
        row = {(k or "").strip().lower(): v for k, v in row.items() if k is not None}
        name = (row.get("name") or "").strip()
        if name:
            db.upsert_base_rate(name, _num(row.get("value")),
                                _num(row.get("delta_1d")), _num(row.get("delta_1m")))
            n += 1
    return RedirectResponse(f"/market?imported=rates:{n}", status_code=303)


@app.post("/market/comps/import")
async def comps_import(file: UploadFile, _=Depends(require_auth)):
    """CSV: asset_type,state,loan_purpose,capital_provider,ltv,rate,term_years,amort_years,recourse,issued,notes."""
    reader = csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig", errors="replace")))
    n = 0
    for row in reader:
        # `if k is not None` drops csv.DictReader's restkey bucket (a LIST of the
        # surplus cells when a row has extra columns), which would crash .strip().
        row = {(k or "").strip().lower(): (v if isinstance(v, str) else "").strip()
               for k, v in row.items() if k is not None}
        if not any(row.values()):
            continue
        db.add_loan_comp({
            "asset_type": row.get("asset_type"), "state": row.get("state"),
            "loan_purpose": row.get("loan_purpose"), "capital_provider": row.get("capital_provider"),
            "ltv": _num(row.get("ltv")), "rate": _num(row.get("rate")),
            "term_years": _num(row.get("term_years")), "amort_years": _num(row.get("amort_years")),
            "recourse": row.get("recourse"), "issued": row.get("issued"), "notes": row.get("notes")})
        n += 1
    db.add_activity("system", f"Imported {n} loan comps from {file.filename}")
    return RedirectResponse(f"/market?imported=comps:{n}", status_code=303)


@app.delete("/market/comp/{comp_id}", response_class=HTMLResponse)
def comp_delete(request: Request, comp_id: int, _=Depends(require_auth)):
    db.delete_loan_comp(comp_id)
    return templates.TemplateResponse("_comps.html", {"request": request, "comps": db.list_loan_comps()})
