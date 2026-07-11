"""Lender matching — the open version of Lev's "match your deal to the right
capital" over 7,000 lender profiles. Pure local scoring over YOUR lender book
(db.lender); no external data, no key. A deal + a lender -> a 0-100 fit score with
explainable reasons. Optional AI pros/cons layer on top (ai.match_rationale)."""


def _loan_size(deal: dict) -> int | None:
    """The loan the deal actually needs — what a lender's min/max box is measured
    against. Prefer the stated loan; else size it from price × LTV (a 60%-LTV deal on a
    $60M asset is a $36M loan, not a $60M one — using the price would wrongly bust a
    $50M-max lender). Fall back to the bare price only when there's no LTV to size with."""
    loan = deal.get("loan_amount")
    if loan:
        return loan
    price, ltv = deal.get("purchase_price"), deal.get("ltv")
    if price and ltv:
        return round(price * ltv / 100)
    return price


def score(deal: dict, lender: dict) -> dict:
    """Return {score, reasons, disqualified}. A hard box violation (wrong property
    type, deal size out of range, paused appetite) disqualifies rather than just
    lowering the score — those are deal-killers, not soft preferences."""
    reasons: list[str] = []
    pts = 0
    disq = False

    # property type — a hard box: lenders that list types and don't cover this one are out.
    ptypes = lender.get("property_types") or []
    dtype = deal.get("property_type")
    if ptypes and dtype:
        if dtype in ptypes:
            pts += 35; reasons.append(f"covers {dtype}")
        else:
            disq = True; reasons.append(f"does not lend on {dtype}")
    elif not ptypes:
        pts += 10; reasons.append("no property-type restriction listed")

    # loan size — a hard box when the lender states a range.
    size = _loan_size(deal)
    lo, hi = lender.get("min_loan"), lender.get("max_loan")
    if size:
        if lo and size < lo:
            disq = True; reasons.append(f"below {lender['name']}'s ${lo:,} minimum")
        elif hi and size > hi:
            disq = True; reasons.append(f"above ${hi:,} maximum")
        elif lo or hi:
            pts += 30; reasons.append(f"${size:,} is in range")

    # geography — soft: national ("US") or the deal's state present is a plus.
    geos = lender.get("geographies") or []
    state = deal.get("state")
    if geos:
        if "US" in geos or (state and state.upper() in [g.upper() for g in geos]):
            pts += 20; reasons.append("lends in this market")
        elif state:
            reasons.append(f"no stated coverage in {state}")
    else:
        pts += 5

    # leverage — soft: their max LTV clears the deal's ask.
    max_ltv, ltv = lender.get("max_ltv"), deal.get("ltv")
    if max_ltv and ltv:
        if ltv <= max_ltv:
            pts += 10; reasons.append(f"{ltv}% LTV within their {max_ltv}% max")
        else:
            reasons.append(f"{ltv}% LTV exceeds their {max_ltv}% max")

    # appetite — active is a plus, paused is a hard stop.
    appetite = (lender.get("appetite") or "active").lower()
    if appetite == "active":
        pts += 5; reasons.append("actively quoting")
    elif appetite == "paused":
        disq = True; reasons.append("currently paused")

    return {"score": 0 if disq else min(pts, 100), "reasons": reasons, "disqualified": disq}


def rank(deal: dict, lenders: list[dict], include_disqualified: bool = False) -> list[dict]:
    """Score every lender for the deal, best first. Disqualified lenders are dropped
    unless asked for (so the user can see why a name they expected didn't match)."""
    out = []
    for l in lenders:
        s = score(deal, l)
        if s["disqualified"] and not include_disqualified:
            continue
        out.append({**l, **s})
    out.sort(key=lambda x: (x["disqualified"], -x["score"]))
    return out


def demo() -> None:
    deal = {"name": "Harbor Pointe", "property_type": "Multifamily", "state": "TX",
            "loan_amount": 12_000_000, "ltv": 65.0}
    good = {"name": "Agency Shop", "property_types": ["Multifamily"], "geographies": ["US"],
            "min_loan": 5_000_000, "max_loan": 50_000_000, "max_ltv": 75.0, "appetite": "active"}
    wrong_type = {"name": "Office Only", "property_types": ["Office"], "min_loan": 1_000_000,
                  "max_loan": 100_000_000, "appetite": "active"}
    too_big = {"name": "Small Balance", "property_types": ["Multifamily"], "min_loan": 1_000_000,
               "max_loan": 5_000_000, "appetite": "active"}

    assert score(deal, good)["score"] >= 90, score(deal, good)
    assert score(deal, wrong_type)["disqualified"], "wrong property type must disqualify"
    assert score(deal, too_big)["disqualified"], "deal above max_loan must disqualify"

    ranked = rank(deal, [good, wrong_type, too_big])
    assert [r["name"] for r in ranked] == ["Agency Shop"], ranked  # only the fit survives
    assert len(rank(deal, [good, wrong_type], include_disqualified=True)) == 2

    # loan sizing: a deal with only price + LTV must be measured on the SIZED loan
    # (price×LTV), not the raw price — else a fitting lender is wrongly disqualified.
    price_deal = {"property_type": "Multifamily", "state": "TX",
                  "purchase_price": 60_000_000, "ltv": 60.0}   # → $36M loan
    mid = {"name": "Mid Fund", "property_types": ["Multifamily"], "geographies": ["US"],
           "min_loan": 5_000_000, "max_loan": 50_000_000, "appetite": "active"}
    assert not score(price_deal, mid)["disqualified"], "sized loan (36M) is within the 50M max"
    assert _loan_size(price_deal) == 36_000_000, _loan_size(price_deal)
    print("matching.demo OK")


if __name__ == "__main__":
    demo()
