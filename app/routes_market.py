"""Market — the capital-markets hub. Three things commercial platforms sell as a proprietary
data moat, here running on data you own:
  • Lender matching   — rank your lender book against a deal (matching.py)
  • Underwriting model — build an Excel pro forma / DSCR / debt sizing (underwriting.py)
  • Market reference   — your Base rates + Recent-terms (closed-loan) comps, BYO/importable

The commercial edge is a live feed of thousands of lenders and loan comps + rate
benchmarks; the honest trade here is you maintain the reference data (seeded, then
edit/import your own)."""
from io import BytesIO  # imported directly: the xlsx route has an `io` query param that shadows the module

from fastapi import Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from . import ai, csvimport, db, matching, underwriting
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
    placed = {p["lender_name"] for p in db.list_placements(deal_id)}
    return templates.TemplateResponse("_match.html", {
        "request": request, "deal": deal, "matches": ranked, "excluded": excluded,
        "placed": placed})


@app.get("/deal/{deal_id}/comps", response_class=HTMLResponse)
def deal_comps(request: Request, deal_id: int, _=Depends(require_auth)):
    """Similar closed loans for this deal → comparable pricing off your own comp set."""
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    comps = db.list_loan_comps(limit=100000)
    ranked = matching.rank_comps(deal, comps)
    pricing = matching.comp_pricing(deal, comps)
    return templates.TemplateResponse("_deal_comps.html", {
        "request": request, "deal": deal, "comps": ranked, "pricing": pricing,
        "total": len(comps)})


@app.post("/deal/{deal_id}/match/{lender_id}/why", response_class=HTMLResponse)
def match_why(request: Request, deal_id: int, lender_id: int, _=Depends(require_auth)):
    deal, lender = db.get_deal(deal_id), db.get_lender(lender_id)
    if not deal or not lender:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal or lender."})
    text = ai.match_rationale(deal, lender) or "Add an Anthropic key in Settings for AI pros/cons."
    return templates.TemplateResponse(
        "_ai_text.html", {"request": request, "label": lender["name"], "text": text})


# --- underwriting model ------------------------------------------------------

def _assumptions(amort: int, growth: float, hold: int, io_flag: str,
                 exitcap: str = "", cost: float = 2.0) -> dict:
    out = {"amort_years": max(1, amort), "noi_growth_pct": growth,
           "hold_years": min(max(1, hold), 30), "interest_only": io_flag == "1",
           "sale_cost_pct": max(0.0, cost)}
    ec = (exitcap or "").strip()
    if ec:
        try:
            out["exit_cap_pct"] = float(ec)
        except ValueError:
            pass
    return out


def _sensitivity_grid(deal: dict, m: dict, kw: dict) -> dict | None:
    """A rate × cap DSCR grid centered on the deal's own rate/cap (±). Needs both a
    working rate and a cap (or a price to derive NOI) to be meaningful."""
    rate = m.get("interest_rate")
    cap = m.get("cap_rate")
    if rate is None or cap is None or not deal.get("purchase_price"):
        return None
    rates = [round(rate + d, 2) for d in (-1.0, -0.5, 0.0, 0.5, 1.0) if rate + d > 0]
    caps = [round(cap + d, 2) for d in (-1.0, -0.5, 0.0, 0.5, 1.0) if cap + d > 0]
    grid_kw = {k: v for k, v in kw.items() if k in
               ("amort_years", "noi_growth_pct", "hold_years", "interest_only")}
    return underwriting.sensitivity(deal, rates, caps, **grid_kw)


@app.get("/deal/{deal_id}/underwrite", response_class=HTMLResponse)
def deal_underwrite(request: Request, deal_id: int, amort: int = 30, growth: float = 3.0,
                    hold: int = 5, io: str = "", exitcap: str = "", cost: float = 2.0,
                    _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    # No activity row here — the assumptions form re-hits this on every knob change
    # (hx-trigger="change"), which would flood the feed. The .xlsx export logs once.
    kw = _assumptions(amort, growth, hold, io, exitcap, cost)
    m = underwriting.compute(deal, **kw)
    grid = _sensitivity_grid(deal, m, kw)
    return templates.TemplateResponse(
        "_underwrite.html", {"request": request, "deal": deal, "m": m, "grid": grid})


@app.get("/deal/{deal_id}/scenarios", response_class=HTMLResponse)
def deal_scenarios(request: Request, deal_id: int, base: str = "", spread: str = "",
                   _=Depends(require_auth)):
    """Side-by-side loan structures for a deal. `base` is a base_rate id + `spread` bps →
    an index+spread priced scenario (rate-based pricing off the base-rate table)."""
    deal = db.get_deal(deal_id)
    if not deal:
        return templates.TemplateResponse("_error.html", {"request": request, "msg": "Unknown deal."})
    base_rate, base_name, spread_bps = None, "", None
    rates = db.list_base_rates()
    if base.isdigit():
        r = next((x for x in rates if x["id"] == int(base)), None)
        if r and r["value"] is not None:
            base_rate, base_name = r["value"], r["name"]
            spread_bps = _num(spread) if spread.strip() else 250.0
    scenarios = underwriting.loan_scenarios(deal, base_rate, spread_bps, base_name)
    return templates.TemplateResponse("_scenarios.html", {
        "request": request, "deal": deal, "scenarios": scenarios, "rates": rates,
        "base": base, "spread": spread or "250"})


@app.get("/deal/{deal_id}/model.xlsx")
def deal_model_xlsx(deal_id: int, amort: int = 30, growth: float = 3.0, hold: int = 5,
                    io: str = "", exitcap: str = "", cost: float = 2.0,
                    _=Depends(require_auth)):
    deal = db.get_deal(deal_id)
    if not deal:
        return HTMLResponse("Unknown deal.", status_code=404)
    kw = _assumptions(amort, growth, hold, io, exitcap, cost)
    m = underwriting.compute(deal, **kw)
    data = underwriting.workbook(deal, m, _sensitivity_grid(deal, m, kw))
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


def _filter_comps(comps: list[dict], asset: str, state: str, q: str) -> list[dict]:
    asset, state, q = asset.strip().lower(), state.strip().upper(), q.strip().lower()
    out = []
    for c in comps:
        if asset and (c.get("asset_type") or "").lower() != asset:
            continue
        if state and (c.get("state") or "").upper() != state:
            continue
        if q and q not in " ".join(str(v) for v in c.values()).lower():
            continue
        out.append(c)
    return out


@app.get("/market/comps", response_class=HTMLResponse)
def market_comps(request: Request, asset: str = "", state: str = "", q: str = "",
                 _=Depends(require_auth)):
    """Filtered Recent-terms table (HTMX partial) — asset type, state, free text."""
    comps = _filter_comps(db.list_loan_comps(limit=100000), asset, state, q)
    return templates.TemplateResponse("_comps.html", {"request": request, "comps": comps})


@app.post("/market/rate/{rate_id}", response_class=HTMLResponse)
def rate_edit(request: Request, rate_id: int, value: str = Form(""),
              delta_1d: str = Form(""), delta_1m: str = Form(""), _=Depends(require_auth)):
    """Inline-edit one base rate's value/deltas by name (rates are keyed by name)."""
    rates = {r["id"]: r for r in db.list_base_rates()}
    r = rates.get(rate_id)
    if r:
        db.upsert_base_rate(r["name"], _num(value) if value.strip() else r["value"],
                            _num(delta_1d) if delta_1d.strip() else r["delta_1d"],
                            _num(delta_1m) if delta_1m.strip() else r["delta_1m"])
    return templates.TemplateResponse("_rates.html", {"request": request, "base_rates": db.list_base_rates()})


def _num(v):
    v = (v or "").strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(v)
    except ValueError:
        return None


@app.post("/market/rates/import")
async def rates_import(file: UploadFile, _=Depends(require_auth)):
    """CSV: name,value,delta_1d,delta_1m (upsert by name)."""
    n = 0
    for row in csvimport.rows(await file.read()):
        name = (row.get("name") or "").strip()
        if name:
            db.upsert_base_rate(name, _num(row.get("value")),
                                _num(row.get("delta_1d")), _num(row.get("delta_1m")))
            n += 1
    return RedirectResponse(f"/market?imported=rates:{n}", status_code=303)


@app.post("/market/comps/import")
async def comps_import(file: UploadFile, _=Depends(require_auth)):
    """CSV: asset_type,state,loan_purpose,capital_provider,ltv,rate,term_years,amort_years,recourse,issued,notes."""
    n = 0
    for row in csvimport.rows(await file.read()):
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
