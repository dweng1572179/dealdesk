"""Export & backup — take your data out. Deals/pipeline → CSV, and a one-click SQLite
backup you can restore. No lock-in: it's your box, your file.

CSV (not xlsx) for the tabular exports: stdlib `csv`, opens in Excel/Sheets, zero deps.
The backup is the whole SQLite file via the sqlite3 backup API (consistent even with
WAL writes in flight), which is the actual system of record — one file is everything."""
import csv
import io

from fastapi import Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import db
from .app import app, require_auth, templates
from .config import settings

# Columns exported per deal, in a sensible reading order.
_DEAL_EXPORT = ["id", "name", "pipeline", "stage", "deal_type", "property_type",
                "address", "city", "state", "sponsor", "purchase_price", "loan_amount",
                "ltv", "interest_rate", "dscr", "cap_rate", "noi", "lender_name",
                "status", "notes", "created_at", "updated_at"]


def _csv_response(rows: list[dict], cols: list[str], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c) for c in cols})
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/export/deals.csv")
def export_deals(pipeline: str = "", _=Depends(require_auth)):
    deals = db.list_deals(pipeline or None)
    name = f"deals_{pipeline}.csv" if pipeline else "deals.csv"
    return _csv_response(deals, _DEAL_EXPORT, name)


@app.get("/export/lenders.csv")
def export_lenders(_=Depends(require_auth)):
    rows = []
    for l in db.list_lenders():
        r = dict(l)
        # list columns back to the same ';'-joined format the importer accepts
        for k in ("loan_types", "property_types", "geographies"):
            r[k] = ";".join(l.get(k) or [])
        rows.append(r)
    cols = ["name", "contact_name", "email", "phone", "loan_types", "property_types",
            "geographies", "min_loan", "max_loan", "max_ltv", "appetite", "notes"]
    return _csv_response(rows, cols, "lenders.csv")


@app.get("/export/contacts.csv")
def export_contacts(_=Depends(require_auth)):
    cols = ["name", "org", "role", "email", "phone", "company_name", "deal_name"]
    return _csv_response(db.list_contacts(), cols, "contacts.csv")


@app.get("/export/comps.csv")
def export_comps(_=Depends(require_auth)):
    cols = ["asset_type", "state", "loan_purpose", "capital_provider", "ltv", "rate",
            "term_years", "amort_years", "recourse", "issued", "notes"]
    return _csv_response(db.list_loan_comps(limit=100000), cols, "loan_comps.csv")


# --- SQLite backup / restore -------------------------------------------------

@app.get("/backup/dealdesk.db")
def backup_db(_=Depends(require_auth)):
    """The whole workspace as one .db file. Uses sqlite3's online backup API into an
    in-memory DB, then serialize() to bytes — a consistent snapshot even mid-write
    (WAL), unlike copying the file off disk while the app has it open."""
    import sqlite3
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst)                 # consistent page-by-page copy
        blob = dst.serialize()          # py3.11+: the raw SQLite file image as bytes
    finally:
        src.close(); dst.close()
    return Response(
        content=blob, media_type="application/x-sqlite3",
        headers={"Content-Disposition": 'attachment; filename="dealdesk-backup.db"'})


@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, restored: str = "", _=Depends(require_auth)):
    ctx = {"request": request, "restored": restored or None}
    from .app import base_ctx
    ctx |= base_ctx(request)
    return templates.TemplateResponse("backup.html", ctx)


@app.post("/backup/restore")
async def restore_db(request: Request, file: UploadFile, _=Depends(require_auth)):
    """Replace the live DB with an uploaded backup. Validates it's a real SQLite file
    with a `deal` table before overwriting, and keeps a .bak of the current DB."""
    import os
    import sqlite3
    raw = await file.read()
    if raw[:16] != b"SQLite format 3\x00":
        return templates.TemplateResponse("_error.html", {
            "request": request, "msg": "That is not a SQLite database file."})
    # Stage the upload NEXT TO the live DB (same directory → same filesystem), so the
    # final os.replace is atomic and can't fail cross-device (a /tmp temp file often
    # lives on a different mount than the DB). Validate the staged copy before swapping.
    staged = settings.db_path + ".restore.tmp"
    try:
        with open(staged, "wb") as f:
            f.write(raw)
        probe = sqlite3.connect(staged)
        try:
            tables = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            probe.close()
        if "deal" not in tables:
            return templates.TemplateResponse("_error.html", {
                "request": request, "msg": "This SQLite file is not a DealDesk backup (no deal table)."})
        # WAL sidecars of the OLD db must go, or a stale -wal masks the restored data.
        for suffix in ("-wal", "-shm"):
            side = settings.db_path + suffix
            if os.path.exists(side):
                os.remove(side)
        if os.path.exists(settings.db_path):
            os.replace(settings.db_path, settings.db_path + ".bak")
        os.replace(staged, settings.db_path)   # same-dir → atomic, no cross-device error
    finally:
        if os.path.exists(staged):
            os.remove(staged)
    db.init_db()   # re-apply pragmas + migrations to the restored file
    db.add_activity("system", f"Restored workspace from {file.filename}")
    return RedirectResponse("/backup?restored=1", status_code=303)
