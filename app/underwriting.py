"""Underwriting model builder — Lev's headline agent deliverable ("provide a T-12 +
Rent Roll → build an Excel underwriting model: pro forma, DSCR, debt sizing"). Here
it's deterministic finance math (no LLM, no key) over a deal's fields, rendered to a
real .xlsx via openpyxl. `compute()` is pure + tested; `workbook()` just formats it.

The assumptions (amortization, NOI growth, hold, interest-only) are knobs, not
hardcodes — real underwriting needs to be tuned, so they're inputs with defaults."""
import io


def annual_debt_service(loan: float | None, rate_pct: float | None,
                        amort_years: int = 30, interest_only: bool = False) -> float | None:
    """Annual debt service on a fully-amortizing (or IO) loan. None if inputs missing.
    Guard the rate on presence (is None), not truthiness — a real 0% loan (seller
    financing / DPA) must reach the r==0 amortization branch, not return None."""
    if not loan or rate_pct is None:
        return None
    if interest_only:
        return loan * rate_pct / 100
    r = rate_pct / 100 / 12
    n = amort_years * 12
    pmt = loan / n if r == 0 else loan * r / (1 - (1 + r) ** (-n))
    return pmt * 12


def remaining_balance(loan: float | None, rate_pct: float | None, amort_years: int,
                      years_elapsed: int, interest_only: bool = False) -> float | None:
    """Loan balance still owed after `years_elapsed` of payments — what gets paid off at
    sale/refi. IO amortizes nothing (full principal remains); a 0% loan pays down
    straight-line; otherwise the standard amortization balance."""
    if not loan or rate_pct is None:
        return None
    if interest_only:
        return float(loan)
    r = rate_pct / 100 / 12
    n = amort_years * 12
    p = min(years_elapsed * 12, n)
    if r == 0:
        return loan * (n - p) / n
    return loan * ((1 + r) ** n - (1 + r) ** p) / ((1 + r) ** n - 1)


def irr(cashflows: list[float]) -> float | None:
    """Internal rate of return of a stream (index 0 = today's outflow), as a decimal
    (0.15 = 15%). Bisection — no numpy. Returns None when the stream never crosses zero
    in a sane range (e.g. all-negative, or a return so extreme it's not meaningful)."""
    def npv(rate: float) -> float:
        return sum(cf / (1 + rate) ** i for i, cf in enumerate(cashflows))

    lo, hi = -0.9999, 100.0     # -100% to +10,000%
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo == 0:
        return lo
    if (f_lo > 0) == (f_hi > 0):
        return None             # no sign change -> no root in range
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return (lo + hi) / 2


def price_from_base(base_value: float | None, spread_bps: float | None) -> float | None:
    """Quote a coupon off a base-rate benchmark + a spread in basis points — the
    'index + spread' pricing every term sheet uses. 4.15% Treasury + 250 bps = 6.65%."""
    if base_value is None or spread_bps is None:
        return None
    return round(base_value + spread_bps / 100, 3)


def compute(deal: dict, amort_years: int = 30, noi_growth_pct: float = 3.0,
            hold_years: int = 5, interest_only: bool = False,
            exit_cap_pct: float | None = None, sale_cost_pct: float = 2.0) -> dict:
    """Derive the underwriting metrics + a multi-year pro forma from a deal. Fills
    the gaps investors leave: NOI from price×cap, loan from price×LTV, and vice-versa.

    Adds an exit at year `hold_years`: sale value = forward NOI / exit cap (defaults to
    the entry cap — no cap-rate movement assumed), less selling costs and loan payoff,
    which drives a levered IRR and equity multiple over the hold."""
    price = deal.get("purchase_price")
    loan = deal.get("loan_amount")
    ltv = deal.get("ltv")
    rate = deal.get("interest_rate")
    cap = deal.get("cap_rate")
    noi = deal.get("noi")

    # fill in what's derivable
    if noi is None and price and cap:
        noi = round(price * cap / 100)
    if loan is None and price and ltv:
        loan = round(price * ltv / 100)
    if ltv is None and loan and price:
        ltv = round(loan / price * 100, 1)
    if cap is None and noi and price:
        cap = round(noi / price * 100, 2)

    ads = annual_debt_service(loan, rate, amort_years, interest_only)
    # has_ds: debt service is KNOWN (could legitimately be 0.0 on a 0% interest-only
    # loan). Guarding on truthiness alone blanks a real $0 debt service; guard division
    # on `ads` (can't divide by 0 → DSCR undefined) but presence-guard the subtractions.
    has_ds = ads is not None
    dscr = round(noi / ads, 2) if noi and ads else None
    debt_yield = round(noi / loan * 100, 2) if noi and loan else None
    # presence-guard so an all-cash deal (loan == 0) reports equity == full price
    equity = price - loan if price is not None and loan is not None else None
    # equity > 0 so an over-leveraged deal shows "—", not a sign-flipped positive return
    coc = round((noi - ads) / equity * 100, 2) if noi and has_ds and equity and equity > 0 else None

    projection = []
    if noi:
        for y in range(1, hold_years + 1):
            noi_y = round(noi * (1 + noi_growth_pct / 100) ** (y - 1))
            cf = round(noi_y - ads) if has_ds else None
            projection.append({
                "year": y, "noi": noi_y, "debt_service": round(ads) if has_ds else None,
                "cash_flow": cf, "dscr": round(noi_y / ads, 2) if ads else None})

    # --- exit / return analysis -------------------------------------------------
    # Sale on forward NOI (the buyer prices the year-after-sale income): NOI grown one
    # year past the hold, capped at the exit cap. Net proceeds pay selling costs + the
    # remaining loan balance; the levered cash-flow stream then gives IRR + multiple.
    exit_cap = exit_cap_pct if exit_cap_pct is not None else cap
    exit_info = None
    returns = {"irr_pct": None, "equity_multiple": None, "profit": None}
    # Only model the exit when the leverage is actually computable: either there's no
    # loan (all-cash) or we have a rate to size debt service AND the payoff. A loan with
    # no rate would otherwise get payoff=0 — the loan silently vanishes at sale and IRR /
    # equity multiple balloon, even though equity was booked net of that loan.
    loan_known = (not loan) or (rate is not None)
    if noi and exit_cap and equity is not None and equity > 0 and loan_known:
        forward_noi = round(noi * (1 + noi_growth_pct / 100) ** hold_years)
        sale_value = round(forward_noi / exit_cap * 100)
        sale_costs = round(sale_value * sale_cost_pct / 100)
        payoff = remaining_balance(loan, rate, amort_years, hold_years, interest_only)
        payoff = round(payoff) if payoff is not None else 0
        net_proceeds = sale_value - sale_costs - payoff
        # levered stream: -equity at t0, annual cash flow years 1..hold, + net proceeds at exit
        flows = [-equity]
        for p in projection:
            cf = p["cash_flow"] if p["cash_flow"] is not None else p["noi"]
            flows.append(cf)
        flows[-1] += net_proceeds
        total_dist = sum(flows[1:])
        r_irr = irr(flows)
        returns = {
            "irr_pct": round(r_irr * 100, 1) if r_irr is not None else None,
            "equity_multiple": round(total_dist / equity, 2) if equity else None,
            "profit": round(total_dist - equity),
        }
        exit_info = {"exit_cap_pct": exit_cap, "forward_noi": forward_noi,
                     "sale_value": sale_value, "sale_costs": sale_costs,
                     "loan_payoff": payoff, "net_proceeds": net_proceeds,
                     "sale_cost_pct": sale_cost_pct}

    return {
        "assumptions": {"amort_years": amort_years, "noi_growth_pct": noi_growth_pct,
                        "hold_years": hold_years, "interest_only": interest_only,
                        "exit_cap_pct": exit_cap, "sale_cost_pct": sale_cost_pct},
        "purchase_price": price, "loan_amount": loan, "ltv": ltv, "interest_rate": rate,
        "cap_rate": cap, "noi": noi, "equity": equity,
        "annual_debt_service": round(ads) if has_ds else None,
        "dscr": dscr, "debt_yield_pct": debt_yield, "cash_on_cash_pct": coc,
        "projection": projection, "exit": exit_info, "returns": returns,
    }


def loan_scenarios(deal: dict, base_rate: float | None = None, spread_bps: float | None = None,
                   base_name: str = "") -> list[dict]:
    """Compute the deal under several loan STRUCTURES for a side-by-side comparison — the
    lender/scenario table a broker builds by hand. Each entry is {label, model}. Always
    includes the deal as entered, an interest-only variant, and a +100bps rate stress;
    adds a base-rate-priced quote (index + spread) when one is given."""
    out = [{"label": "As entered", "model": compute(deal)},
           {"label": "Interest-only", "model": compute(deal, interest_only=True)}]
    rate = deal.get("interest_rate")
    if rate is not None:
        out.append({"label": "Rate +100 bps",
                    "model": compute({**deal, "interest_rate": rate + 1.0})})
    priced = price_from_base(base_rate, spread_bps)
    if priced is not None:
        label = (f"{base_name or 'Base'} {base_rate}% + {spread_bps:g} bps = {priced}%")
        out.append({"label": label, "model": compute({**deal, "interest_rate": priced})})
    return out


def sensitivity(deal: dict, rates: list[float], caps: list[float], **kw) -> dict:
    """A rate × cap grid of DSCR — how coverage holds up as pricing and exit values
    move. Rows are interest rates, columns are cap rates; each cell recomputes the
    model with that (rate, cap) pair. The one table an underwriter always wants."""
    rows = []
    for r in rates:
        cells = []
        for c in caps:
            d = {**deal, "interest_rate": r, "cap_rate": c}
            # a cap-driven NOI needs price; if the deal states NOI directly it's fixed,
            # so recompute NOI from price×cap when price is present to make the grid move.
            if d.get("purchase_price"):
                d.pop("noi", None)
            m = compute(d, **kw)
            cells.append(m["dscr"])
        rows.append({"rate": r, "cells": cells})
    return {"rates": rates, "caps": caps, "rows": rows}


def workbook(deal: dict, m: dict, grid: dict | None = None) -> bytes:
    """Render the computed model to an .xlsx (bytes). openpyxl only; no template file."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Underwriting"
    bold = Font(bold=True)

    def money(v):
        return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"

    def pct(v):
        return f"{v}%" if isinstance(v, (int, float)) else "—"

    ex = m.get("exit") or {}
    ret = m.get("returns") or {}
    rows = [
        (deal.get("name", "Deal"), ""),
        (f"{deal.get('property_type') or '—'} · {deal.get('city') or ''} {deal.get('state') or ''}".strip(), ""),
        ("", ""),
        ("Sources & Uses", ""),
        ("Purchase price", money(m["purchase_price"])),
        ("Loan amount", money(m["loan_amount"])),
        ("Equity required", money(m["equity"])),
        ("LTV", pct(m["ltv"])),
        ("", ""),
        ("Debt sizing", ""),
        ("Interest rate", pct(m["interest_rate"])),
        ("Amortization (yrs)", "Interest-only" if m["assumptions"]["interest_only"] else m["assumptions"]["amort_years"]),
        ("Annual debt service", money(m["annual_debt_service"])),
        ("DSCR", m["dscr"] if m["dscr"] is not None else "—"),
        ("Debt yield", pct(m["debt_yield_pct"])),
        ("", ""),
        ("Operating returns", ""),
        ("Year-1 NOI", money(m["noi"])),
        ("Cap rate", pct(m["cap_rate"])),
        ("Cash-on-cash", pct(m["cash_on_cash_pct"])),
        ("", ""),
        (f"Exit (year {m['assumptions']['hold_years']})", ""),
        ("Exit cap rate", pct(m["assumptions"].get("exit_cap_pct"))),
        ("Sale value", money(ex.get("sale_value"))),
        ("Selling costs", money(ex.get("sale_costs"))),
        ("Loan payoff", money(ex.get("loan_payoff"))),
        ("Net sale proceeds", money(ex.get("net_proceeds"))),
        ("Levered IRR", pct(ret.get("irr_pct"))),
        ("Equity multiple", f"{ret['equity_multiple']}x" if ret.get("equity_multiple") is not None else "—"),
        ("Total profit", money(ret.get("profit"))),
    ]
    section_titles = ("Sources & Uses", "Debt sizing", "Operating returns")
    for i, (a, b) in enumerate(rows, start=1):
        ws.cell(row=i, column=1, value=a)
        ws.cell(row=i, column=2, value=b)
        if a in section_titles or a.startswith("Exit (year") or (b == "" and a and i <= 2):
            ws.cell(row=i, column=1).font = bold

    r = len(rows) + 2
    if m["projection"]:
        ws.cell(row=r, column=1, value=f"Pro forma ({m['assumptions']['noi_growth_pct']}% NOI growth)").font = bold
        headers = ["Year", "NOI", "Debt service", "Cash flow", "DSCR"]
        for c, h in enumerate(headers, start=1):
            ws.cell(row=r + 1, column=c, value=h).font = bold
        for rr, p in enumerate(m["projection"], start=r + 2):
            for c, key in enumerate(("year", "noi", "debt_service", "cash_flow", "dscr"), start=1):
                ws.cell(row=rr, column=c, value=p[key])
        r = r + 2 + len(m["projection"]) + 1

    if grid and grid.get("rows"):
        ws.cell(row=r, column=1, value="DSCR sensitivity (rate ↓ × cap →)").font = bold
        ws.cell(row=r + 1, column=1, value="Rate \\ Cap").font = bold
        for c, cap in enumerate(grid["caps"], start=2):
            ws.cell(row=r + 1, column=c, value=f"{cap}%").font = bold
        for rr, row in enumerate(grid["rows"], start=r + 2):
            ws.cell(row=rr, column=1, value=f"{row['rate']}%").font = bold
            for c, cell in enumerate(row["cells"], start=2):
                ws.cell(row=rr, column=c, value=cell if cell is not None else "—")

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 16
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def demo() -> None:
    # 10M loan @ 6% / 30yr amortizing → ~719,463/yr debt service
    ads = annual_debt_service(10_000_000, 6.0, 30)
    assert abs(ads - 719_463) < 50, ads
    # interest-only is just rate × principal
    assert annual_debt_service(10_000_000, 6.0, 30, interest_only=True) == 600_000

    deal = {"name": "Harbor Pointe", "purchase_price": 41_000_000, "ltv": 68.0,
            "interest_rate": 6.5, "cap_rate": 5.2}
    m = compute(deal)
    assert m["loan_amount"] == round(41_000_000 * 0.68), m["loan_amount"]        # loan from LTV
    assert m["noi"] == round(41_000_000 * 0.052), m["noi"]                        # NOI from cap
    assert m["equity"] == 41_000_000 - m["loan_amount"], m["equity"]
    assert m["dscr"] and m["dscr"] == round(m["noi"] / m["annual_debt_service"], 2)
    assert len(m["projection"]) == 5 and m["projection"][0]["year"] == 1
    # NOI grows 3%/yr by default
    assert m["projection"][1]["noi"] > m["projection"][0]["noi"]
    # missing everything → no crash, all-None metrics
    empty = compute({"name": "TBD"})
    assert empty["dscr"] is None and empty["projection"] == []

    # 0% loan must amortize (annual = loan/amort_years), not return None
    assert abs(annual_debt_service(1_000_000, 0.0, 30) - 1_000_000 / 30) < 1
    assert annual_debt_service(1_000_000, 0.0, 30, interest_only=True) == 0.0
    # a missing (None) rate is still None
    assert annual_debt_service(1_000_000, None, 30) is None

    # all-cash deal (loan 0) → equity = full price, no phantom returns
    cash = compute({"purchase_price": 5_000_000, "loan_amount": 0, "noi": 300_000})
    assert cash["equity"] == 5_000_000 and cash["cash_on_cash_pct"] is None, cash

    # 0% interest-only loan → real $0 debt service (not None): cash flow = NOI, and
    # cash-on-cash is computable; DSCR stays None (coverage is infinite, not a number).
    zio = compute({"purchase_price": 10_000_000, "loan_amount": 5_000_000,
                   "interest_rate": 0.0, "noi": 500_000}, interest_only=True)
    assert zio["annual_debt_service"] == 0, zio
    assert zio["projection"][0]["debt_service"] == 0, zio["projection"][0]
    assert zio["projection"][0]["cash_flow"] == zio["projection"][0]["noi"], zio["projection"][0]
    assert zio["cash_on_cash_pct"] == 10.0, zio   # 500k / 5M equity

    # over-leveraged (equity < 0) must NOT report a positive cash-on-cash
    over = compute({"purchase_price": 10_000_000, "loan_amount": 12_000_000,
                    "noi": 800_000, "interest_rate": 6.0})
    assert over["equity"] == -2_000_000 and over["cash_on_cash_pct"] is None, over

    # --- IRR: a known stream. -100 then 60,60,60 has IRR ~36.3% ---------------
    r = irr([-100, 60, 60, 60])
    assert r is not None and abs(r - 0.363) < 0.005, r
    # a doubling in one year is exactly 100% IRR
    assert abs(irr([-100, 200]) - 1.0) < 1e-3, irr([-100, 200])
    # an all-loss stream has no positive IRR
    assert irr([-100, -10, -10]) is None
    # NPV at the returned IRR is ~0 (self-consistency)
    flows = [-1_000_000, 80_000, 80_000, 80_000, 1_300_000]
    ri = irr(flows)
    npv = sum(cf / (1 + ri) ** i for i, cf in enumerate(flows))
    assert abs(npv) < 1.0, (ri, npv)

    # --- remaining balance: IO keeps full principal; amortizing pays down ------
    assert remaining_balance(10_000_000, 6.0, 30, 5, interest_only=True) == 10_000_000
    bal5 = remaining_balance(10_000_000, 6.0, 30, 5)
    assert 9_200_000 < bal5 < 9_400_000, bal5      # ~5yr into a 30yr @6% ≈ 93% owed
    assert remaining_balance(10_000_000, 6.0, 30, 30) < 1.0   # fully paid at maturity
    assert abs(remaining_balance(3_000_000, 0.0, 30, 10) - 2_000_000) < 1  # 0%: straight-line

    # --- exit / returns: a clean case with hand-checkable numbers -------------
    # $10M price, 6% cap → $600k NOI; 65% LTV IO @6%; 3% growth; sell yr5 at 6% cap.
    ex = compute({"purchase_price": 10_000_000, "cap_rate": 6.0, "ltv": 65.0,
                  "interest_rate": 6.0}, interest_only=True, hold_years=5, sale_cost_pct=0.0)
    assert ex["equity"] == 3_500_000, ex["equity"]                 # 10M - 6.5M loan
    # forward NOI = 600k*1.03^5 ≈ 695.6k; sale = /6% ≈ 11.59M
    assert abs(ex["exit"]["forward_noi"] - 695_562) < 200, ex["exit"]
    assert abs(ex["exit"]["sale_value"] - 11_592_700) < 5_000, ex["exit"]
    assert ex["exit"]["loan_payoff"] == 6_500_000, ex["exit"]      # IO: full loan
    assert ex["returns"]["irr_pct"] is not None and ex["returns"]["equity_multiple"] > 1.0, ex["returns"]
    # exit cap expansion (6% → 7%) must LOWER the sale value and the return
    ex_soft = compute({"purchase_price": 10_000_000, "cap_rate": 6.0, "ltv": 65.0,
                       "interest_rate": 6.0}, interest_only=True, hold_years=5,
                      exit_cap_pct=7.0, sale_cost_pct=0.0)
    assert ex_soft["exit"]["sale_value"] < ex["exit"]["sale_value"], "cap expansion cuts value"
    assert ex_soft["returns"]["irr_pct"] < ex["returns"]["irr_pct"], "cap expansion cuts IRR"
    # no equity (fully leveraged / no price) → no phantom returns
    assert compute({"noi": 500_000})["returns"]["irr_pct"] is None
    # a loan with NO rate must not fabricate an exit (payoff would wrongly be 0, dropping
    # the loan at sale and inflating IRR/equity multiple)
    norate = compute({"purchase_price": 10_000_000, "loan_amount": 5_000_000, "cap_rate": 6.0})
    assert norate["exit"] is None and norate["returns"]["irr_pct"] is None, norate["returns"]

    # --- price_from_base: index + spread --------------------------------------
    assert price_from_base(4.15, 250) == 6.65
    assert price_from_base(None, 250) is None

    # --- loan scenarios: side-by-side structures ------------------------------
    sc_deal = {"purchase_price": 20_000_000, "ltv": 65.0, "cap_rate": 6.0, "interest_rate": 6.5}
    sc = loan_scenarios(sc_deal, base_rate=4.15, spread_bps=250, base_name="UST 10Y")
    labels = [s["label"] for s in sc]
    assert labels[0] == "As entered" and "Interest-only" in labels, labels
    assert any("Rate +100 bps" == l for l in labels), labels
    assert any("6.65%" in l for l in labels), labels   # 4.15 + 250bps priced scenario
    # rate stress lowers DSCR vs as-entered
    base_dscr = sc[0]["model"]["dscr"]
    stress = next(s for s in sc if s["label"] == "Rate +100 bps")["model"]["dscr"]
    assert stress < base_dscr, (base_dscr, stress)
    # interest-only lowers debt service vs amortizing → higher DSCR
    io = next(s for s in sc if s["label"] == "Interest-only")["model"]
    assert io["dscr"] > base_dscr, (base_dscr, io["dscr"])
    # a deal with no rate still returns the base + IO scenarios (no stress/priced)
    assert len(loan_scenarios({"purchase_price": 10_000_000, "ltv": 60.0, "cap_rate": 6.0})) == 2

    # --- sensitivity grid: DSCR falls as the rate rises, rises as cap rises ----
    grid = sensitivity({"purchase_price": 20_000_000, "ltv": 65.0},
                       rates=[5.0, 7.0], caps=[5.0, 6.0])
    d_lowrate = grid["rows"][0]["cells"][0]   # rate 5, cap 5
    d_highrate = grid["rows"][1]["cells"][0]  # rate 7, cap 5
    d_highcap = grid["rows"][0]["cells"][1]   # rate 5, cap 6
    assert d_lowrate > d_highrate, "higher rate → lower DSCR"
    assert d_highcap > d_lowrate, "higher cap (more NOI) → higher DSCR"

    # workbook renders to real xlsx bytes (ZIP magic)
    assert workbook(deal, m)[:2] == b"PK"
    print("underwriting.demo OK")


if __name__ == "__main__":
    demo()
