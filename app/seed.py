"""First-run demo data — sample deals (the ones on Lev's Home screen) + a starter
lender book so matching works out of the box. Runs once, only into an empty DB;
delete the rows or start fresh to clear it. Not real capital sources — replace the
lender book with your own via CRM → Lenders → Import CSV."""
from . import db

_DEALS = [
    {"name": "Harbor Pointe Apartments", "pipeline": "financing", "stage": "In Market",
     "deal_type": "financing", "property_type": "Multifamily", "address": "1200 Harbor Blvd",
     "city": "Austin", "state": "TX", "sponsor": "Cedar Ridge Capital",
     "purchase_price": 41_000_000, "loan_amount": 28_000_000, "ltv": 68.0,
     "dscr": 1.28, "cap_rate": 5.2, "noi": 2_130_000},
    {"name": "6801 Lankershim Blvd", "pipeline": "acquisition", "stage": "Under Contract",
     "deal_type": "acquisition", "property_type": "Office", "address": "6801 Lankershim Blvd",
     "city": "Los Angeles", "state": "CA", "sponsor": "Meridian Partners",
     "purchase_price": 62_500_000, "loan_amount": 40_000_000, "ltv": 64.0,
     "dscr": 1.35, "cap_rate": 6.1},
    {"name": "Riverbend Industrial", "pipeline": "financing", "stage": "Term Sheets",
     "deal_type": "financing", "property_type": "Industrial", "address": "4400 Riverbend Dr",
     "city": "Phoenix", "state": "AZ", "sponsor": "Summit Logistics",
     "purchase_price": 33_000_000, "loan_amount": 22_500_000, "ltv": 68.0,
     "dscr": 1.4, "cap_rate": 5.8, "noi": 1_910_000},
]

_LENDERS = [
    {"name": "Agency Multifamily Shop", "loan_types": ["acquisition", "refinance"],
     "property_types": ["Multifamily"], "geographies": ["US"],
     "min_loan": 5_000_000, "max_loan": 75_000_000, "max_ltv": 80.0, "appetite": "active",
     "notes": "Fannie/Freddie DUS; best pricing on stabilized multifamily."},
    {"name": "Regional Bank — Southwest", "loan_types": ["acquisition", "bridge", "construction"],
     "property_types": ["Multifamily", "Office", "Industrial", "Retail"],
     "geographies": ["TX", "AZ", "NM", "NV"], "min_loan": 2_000_000, "max_loan": 30_000_000,
     "max_ltv": 65.0, "appetite": "selective", "notes": "Recourse; relationship-driven."},
    {"name": "National Debt Fund", "loan_types": ["bridge", "mezz", "construction"],
     "property_types": ["Multifamily", "Office", "Industrial", "Retail", "Hospitality"],
     "geographies": ["US"], "min_loan": 15_000_000, "max_loan": 200_000_000, "max_ltv": 75.0,
     "appetite": "active", "notes": "Non-recourse bridge; quick close, higher coupon."},
    {"name": "Life Co — Core", "loan_types": ["acquisition", "refinance"],
     "property_types": ["Office", "Industrial", "Retail"], "geographies": ["US"],
     "min_loan": 20_000_000, "max_loan": 150_000_000, "max_ltv": 60.0, "appetite": "active",
     "notes": "Low leverage, long fixed term, tight spreads on core assets."},
    {"name": "Industrial Specialist", "loan_types": ["acquisition", "construction"],
     "property_types": ["Industrial"], "geographies": ["US"], "min_loan": 5_000_000,
     "max_loan": 80_000_000, "max_ltv": 70.0, "appetite": "active",
     "notes": "Logistics/last-mile focus."},
    {"name": "Hospitality Credit", "loan_types": ["acquisition", "bridge"],
     "property_types": ["Hospitality"], "geographies": ["US"], "min_loan": 10_000_000,
     "max_loan": 120_000_000, "max_ltv": 65.0, "appetite": "paused",
     "notes": "On pause pending RevPAR recovery."},
]


# Loan-pricing benchmarks. Static seed values — YOU keep these current (Lev streams
# them live; the open version is BYO). Values are illustrative, ~mid-2026.
_BASE_RATES = [
    ("Prime", 6.75, 0.0, -0.25), ("SOFR (o/n)", 3.53, 0.01, -0.12),
    ("1-Mo SOFR", 3.58, 0.0, -0.10), ("Treasury 5-Yr", 3.85, 0.02, 0.09),
    ("Treasury 10-Yr", 4.15, 0.03, 0.14), ("CMT 5-Yr", 3.88, 0.02, 0.10),
    ("CMT 10-Yr", 4.18, 0.03, 0.15), ("SOFR Swap 5-Yr", 3.72, 0.02, 0.08),
    ("SOFR Swap 7-Yr", 3.80, 0.02, 0.10), ("SOFR Swap 10-Yr", 3.95, 0.03, 0.12),
]

# A few closed-loan comps ("Recent terms"). Replace with your own via Market → import.
_COMPS = [
    {"asset_type": "Multifamily", "state": "TX", "loan_purpose": "Acquisition · Permanent",
     "capital_provider": "Agency", "ltv": 70, "rate": 6.35, "term_years": 10, "amort_years": 30,
     "recourse": "Non-recourse", "issued": "2026-05"},
    {"asset_type": "Industrial", "state": "AZ", "loan_purpose": "Refinance · Permanent",
     "capital_provider": "Life Insurance Company", "ltv": 60, "rate": 6.05, "term_years": 10,
     "amort_years": 30, "recourse": "Non-recourse", "issued": "2026-04"},
    {"asset_type": "Office", "state": "CA", "loan_purpose": "Acquisition · Bridge",
     "capital_provider": "Debt Fund", "ltv": 65, "rate": 8.75, "term_years": 3, "amort_years": 0,
     "recourse": "Partial", "issued": "2026-06"},
    {"asset_type": "Retail", "state": "FL", "loan_purpose": "Refinance · Permanent",
     "capital_provider": "Regional Bank", "ltv": 65, "rate": 6.90, "term_years": 5,
     "amort_years": 25, "recourse": "Recourse", "issued": "2026-03"},
    {"asset_type": "Multifamily", "state": "GA", "loan_purpose": "Acquisition · Bridge",
     "capital_provider": "Small Balance Debt Fund", "ltv": 75, "rate": 8.25, "term_years": 3,
     "amort_years": 0, "recourse": "Non-recourse", "issued": "2026-05"},
]

_COMPANIES = [
    {"name": "Cedar Ridge Capital", "type": "Sponsor", "location": "Austin, TX"},
    {"name": "Meridian Partners", "type": "Sponsor", "location": "Los Angeles, CA"},
    {"name": "Summit Logistics", "type": "Sponsor", "location": "Phoenix, AZ"},
]


def _load_demo() -> None:
    """Insert the demo deals + lender book + market data. Assumes an empty workspace."""
    for l in _LENDERS:
        db.upsert_lender(l)
    for d in _DEALS:
        did = db.create_deal(d)
        db.add_activity("system", f"Imported sample deal {d['name']}", did)
    for name, val, d1, d1m in _BASE_RATES:
        db.upsert_base_rate(name, val, d1, d1m)
    for c in _COMPS:
        db.add_loan_comp(c)
    for c in _COMPANIES:
        db.upsert_company(c)
    db.add_task(db.list_deals()[0]["id"], "Circle back with lender on term sheet")
    db.add_activity("system",
                    "Welcome to DealDesk — sample deals, a starter lender book, and market "
                    "reference data loaded. Replace them with your own any time.")


def seed_if_empty() -> None:
    # Sentinel, not an emptiness check: a user who clears the sample data to start
    # clean (deletes every seeded deal AND lender) must NOT get the demo set re-inserted
    # on the next restart. The seed runs exactly once per database.
    from .settings_store import get_flag, set_flag
    if get_flag("seeded") or db.list_deals() or db.list_lenders():
        set_flag("seeded", "1")
        return
    _load_demo()
    set_flag("seeded", "1")   # never re-seed this database, even if the user empties it


# tables holding user/workspace data — everything EXCEPT `setting` (keys, flags) and the
# AI spend ledger (billing history). Deleting a parent cascades to its children, but we
# clear all of them explicitly so the order doesn't matter.
_DATA_TABLES = ["placement", "email", "task", "document", "activity", "contact", "deal",
                "lender", "company", "base_rate", "loan_comp"]


def reset_workspace(reseed: bool = True) -> None:
    """Wipe all workspace data (keeps your saved keys + spend ledger). Optionally reloads
    the demo set. The 'demo/reset' toggle behind Settings — try the app, then start clean."""
    from .settings_store import set_flag
    with db.get_conn() as conn:
        for t in _DATA_TABLES:
            conn.execute(f"DELETE FROM {t}")
    if reseed:
        set_flag("seeded", "0")
        _load_demo()
    set_flag("seeded", "1")   # either way, don't auto-reseed on the next boot


def demo() -> None:
    import os
    import tempfile
    from . import db as _db
    _db.settings.db_path = os.path.join(tempfile.mkdtemp(), "seed.db")
    _db.init_db()
    _load_demo()
    assert _db.list_deals() and _db.list_lenders(), "demo data should load"
    # reset to empty
    reset_workspace(reseed=False)
    assert not _db.list_deals() and not _db.list_lenders() and not _db.list_loan_comps(), "reset should empty it"
    # reset with reseed reloads the demo set
    reset_workspace(reseed=True)
    assert _db.list_deals() and _db.list_lenders(), "reseed should reload demo data"
    print("seed.demo (load + reset + reseed) OK")


if __name__ == "__main__":
    demo()
