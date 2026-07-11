"""End-to-end smoke test over the real HTTP app (FastAPI TestClient), isolated on a
temp DB. No network, no keys — every AI feature runs its rules/template fallback.
Run from the dealdesk/ dir:  python -m tests.test_smoke"""
import os
import tempfile

_TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_TMP, "smoke.db")
os.environ["DEALDESK_PASSWORD"] = "test-pw"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ANTHROPIC_API_KEY"] = ""  # force rules fallback

from fastapi.testclient import TestClient  # noqa: E402

from app.app import app  # noqa: E402


def run() -> None:
    with TestClient(app) as c:
        # unauth -> login
        assert c.get("/", follow_redirects=False).status_code == 303

        # wrong then right password
        assert c.post("/login", data={"password": "nope"}, follow_redirects=False).status_code == 401
        r = c.post("/login", data={"password": "test-pw"}, follow_redirects=False)
        assert r.status_code == 303, r.status_code

        # seed loaded on startup
        home = c.get("/")
        assert home.status_code == 200 and "Harbor Pointe" in c.get("/deals?pipeline=financing").text

        # create a deal -> redirected to its detail page
        r = c.post("/deals", data={"name": "Test Tower", "pipeline": "acquisition",
                                   "property_type": "Office", "city": "Dallas", "state": "TX",
                                   "loan_amount": "9,000,000"}, follow_redirects=True)
        assert r.status_code == 200 and "Test Tower" in r.text
        deal_id = int(r.url.path.rsplit("/", 1)[-1])

        # loan amount parsed through the comma-stripping coercion
        from app import db
        assert db.get_deal(deal_id)["loan_amount"] == 9_000_000

        # edit + stage move
        assert c.post(f"/deal/{deal_id}", data={"name": "Test Tower", "ltv": "65%",
                      "dscr": "1.25"}, follow_redirects=True).status_code == 200
        assert db.get_deal(deal_id)["ltv"] == 65.0
        board = c.post(f"/deal/{deal_id}/stage", data={"stage": "LOI", "pipeline": "acquisition"})
        assert board.status_code == 200 and db.get_deal(deal_id)["stage"] == "LOI"

        # agent (rules fallback) finds the deal by keyword
        a = c.post("/agent", data={"question": "which office deals in dallas"})
        assert a.status_code == 200 and "Test Tower" in a.text

        # task add/toggle
        t = c.post(f"/deal/{deal_id}/task", data={"body": "Call the broker", "due": ""})
        assert "Call the broker" in t.text
        tid = db.list_tasks(deal_id)[0]["id"]
        c.post(f"/task/{tid}/toggle", data={"done": "1"})
        assert db.list_tasks(deal_id)[0]["done"] == 1

        # document upload -> regex term extraction (no key)
        up = c.post(f"/deal/{deal_id}/upload",
                    files={"file": ("ts.txt", b"Loan Amount: $9,000,000. LTV 65%. DSCR 1.25x.", "text/plain")},
                    data={"apply": "1"})
        assert up.status_code == 200 and "9,000,000" in up.text
        assert db.list_documents(deal_id), "document not saved"

        # lender matching against the seeded book
        m = c.get(f"/deal/{deal_id}/match")
        assert m.status_code == 200 and ("/100" in m.text or "No lenders fit" in m.text)

        # underwriting model: preview partial + a real .xlsx download
        uw = c.get(f"/deal/{deal_id}/underwrite")
        assert uw.status_code == 200 and "DSCR" in uw.text
        xlsx = c.get(f"/deal/{deal_id}/model.xlsx")
        assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK", "xlsx not a ZIP"
        assert "spreadsheetml" in xlsx.headers["content-type"]

        # Market page renders seeded base rates + comps; comps import works
        mkt = c.get("/market")
        assert mkt.status_code == 200 and "Base rates" in mkt.text and "Prime" in mkt.text
        # includes an over-wide row (unquoted comma in notes) — must NOT 500 (restkey fix)
        comp_csv = (b"asset_type,state,loan_purpose,rate,ltv,issued,notes\n"
                    b"Hotel,NV,Acquisition,9.1,60,2026-07,great deal, closed fast\n")
        r = c.post("/market/comps/import", files={"file": ("c.csv", comp_csv, "text/csv")}, follow_redirects=True)
        assert r.status_code == 200 and any(x["asset_type"] == "Hotel" for x in db.list_loan_comps())
        # a whitespace-only rate name must not create an empty-named benchmark
        rate_csv = b"name,value\n , 5.0\nTest Bench,4.2\n"
        c.post("/market/rates/import", files={"file": ("r.csv", rate_csv, "text/csv")}, follow_redirects=True)
        names = [r["name"] for r in db.list_base_rates()]
        assert "" not in names and "Test Bench" in names, names

        # CRM companies
        c.post("/crm/company", data={"name": "Acme Sponsor", "type": "Sponsor",
               "location": "Miami, FL"}, follow_redirects=True)
        assert any(co["name"] == "Acme Sponsor" for co in db.list_companies())

        # activity feed + email guardrails (no inbox connected)
        assert c.get("/activity").status_code == 200
        assert "Connect an inbox" in c.post("/inbox/sync").text
        assert "Connect an inbox" in c.post(f"/deal/{deal_id}/email/send",
                                            data={"to": "x@y.com", "subject": "s", "body": "b"}).text

        # CRM: add a contact + a lender, import a CSV
        c.post("/crm/contact", data={"name": "Jane Broker", "role": "broker",
               "email": "jane@brk.com", "deal_id": str(deal_id)}, follow_redirects=True)
        assert db.contact_by_email("jane@brk.com")
        csv = b"name,property_types,min_loan,max_loan,geographies,appetite\nCSV Bank,Office,1000000,20000000,US,active\n"
        imp = c.post("/crm/lenders/import", files={"file": ("l.csv", csv, "text/csv")}, follow_redirects=True)
        assert imp.status_code == 200 and any(l["name"] == "CSV Bank" for l in db.list_lenders())

        # settings save (budget) applies live
        c.post("/settings", data={"monthly_budget_cents": "2500"})
        from app.config import settings
        assert settings.monthly_budget_cents == 2500

    print("test_smoke OK")


if __name__ == "__main__":
    run()
