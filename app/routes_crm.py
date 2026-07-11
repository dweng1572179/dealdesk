"""CRM directory — contacts (brokers/lenders/sponsors) and your lender book. Lenders
import from CSV so you can bring a real capital-provider list (the open answer to
Lev's 7,000 lender profiles: you own the data)."""
import csv
import io

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi import UploadFile

from . import db
from .app import app, base_ctx, require_auth, templates
from .models import LENDER_APPETITE, PROPERTY_TYPES


def _crm_ctx(request: Request, imported: int | None = None) -> dict:
    ctx = base_ctx(request)
    ctx |= {"contacts": db.list_contacts(), "lenders": db.list_lenders(),
            "companies": db.list_companies(), "property_types": PROPERTY_TYPES,
            "appetites": LENDER_APPETITE, "deals": db.list_deals(), "imported": imported}
    return ctx


@app.get("/crm", response_class=HTMLResponse)
def crm(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("crm.html", _crm_ctx(request))


@app.post("/crm/contact")
def contact_add(name: str = Form(...), org: str = Form(""), role: str = Form("other"),
                email: str = Form(""), phone: str = Form(""), deal_id: str = Form(""),
                _=Depends(require_auth)):
    if name.strip():
        db.create_contact({"name": name.strip(), "org": org.strip() or None,
                           "role": role, "email": email.strip() or None,
                           "phone": phone.strip() or None,
                           "deal_id": int(deal_id) if deal_id.strip().isdigit() else None})
    return RedirectResponse("/crm", status_code=303)


@app.post("/crm/company")
def company_add(name: str = Form(...), type: str = Form(""), location: str = Form(""),
                website: str = Form(""), notes: str = Form(""), _=Depends(require_auth)):
    if name.strip():
        db.upsert_company({"name": name.strip(), "type": type.strip() or None,
                           "location": location.strip() or None, "website": website.strip() or None,
                           "notes": notes.strip() or None})
    return RedirectResponse("/crm", status_code=303)


@app.delete("/company/{company_id}", response_class=HTMLResponse)
def company_delete(request: Request, company_id: int, _=Depends(require_auth)):
    db.delete_company(company_id)
    return templates.TemplateResponse(
        "_companies.html", {"request": request, "companies": db.list_companies()})


@app.delete("/contact/{contact_id}", response_class=HTMLResponse)
def contact_delete(request: Request, contact_id: int, deal_id: str = Form(""),
                   _=Depends(require_auth)):
    # A deal page's Contacts panel is deal-scoped and shares the #contacts target,
    # so re-render with the same scope it was rendered at (the button rides deal_id
    # via hx-vals); the /crm page sends none -> global list. Re-pass deal_id so the
    # swapped buttons keep their scope on a second consecutive delete.
    db.delete_contact(contact_id)
    did = int(deal_id) if deal_id.strip().isdigit() else None
    return templates.TemplateResponse(
        "_contacts.html", {"request": request, "contacts": db.list_contacts(did),
                           "deal_id": deal_id if did else ""})


def _split(v: str) -> list[str]:
    return [x.strip() for x in (v or "").replace(";", ",").split(",") if x.strip()]


def _num(v: str):
    v = (v or "").strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return None


@app.post("/crm/lender")
async def lender_add(request: Request, _=Depends(require_auth)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if name:
        db.upsert_lender({
            "name": name, "contact_name": (form.get("contact_name") or "").strip() or None,
            "email": (form.get("email") or "").strip() or None,
            "phone": (form.get("phone") or "").strip() or None,
            "loan_types": _split(form.get("loan_types")),
            "property_types": form.getlist("property_types"),
            "geographies": _split(form.get("geographies")),
            "min_loan": _num(form.get("min_loan")), "max_loan": _num(form.get("max_loan")),
            "max_ltv": _num(form.get("max_ltv")),
            "appetite": form.get("appetite") or "active",
            "notes": (form.get("notes") or "").strip() or None})
    return RedirectResponse("/crm", status_code=303)


@app.delete("/lender/{lender_id}", response_class=HTMLResponse)
def lender_delete(request: Request, lender_id: int, _=Depends(require_auth)):
    db.delete_lender(lender_id)
    return templates.TemplateResponse(
        "_lenders.html", {"request": request, "lenders": db.list_lenders()})


@app.post("/crm/lenders/import", response_class=HTMLResponse)
async def lenders_import(request: Request, file: UploadFile, _=Depends(require_auth)):
    """CSV import. Columns: name, contact_name, email, phone, loan_types, property_types,
    geographies, min_loan, max_loan, max_ltv, appetite, notes. List columns are
    comma/semicolon-separated. Upsert by name, so re-importing updates in place."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    n = 0
    for row in reader:
        # `if k is not None` drops csv.DictReader's restkey bucket (a LIST of surplus
        # cells on an over-wide row), which would otherwise crash .strip().
        row = {(k or "").strip().lower(): (v if isinstance(v, str) else "").strip()
               for k, v in row.items() if k is not None}
        name = row.get("name")
        if not name:
            continue
        db.upsert_lender({
            "name": name, "contact_name": row.get("contact_name"), "email": row.get("email"),
            "phone": row.get("phone"), "loan_types": _split(row.get("loan_types")),
            "property_types": _split(row.get("property_types")),
            "geographies": _split(row.get("geographies")),
            "min_loan": _num(row.get("min_loan")), "max_loan": _num(row.get("max_loan")),
            "max_ltv": _num(row.get("max_ltv")),
            "appetite": row.get("appetite") or "active", "notes": row.get("notes")})
        n += 1
    db.add_activity("system", f"Imported {n} lenders from {file.filename}")
    return templates.TemplateResponse("crm.html", _crm_ctx(request, imported=n))
