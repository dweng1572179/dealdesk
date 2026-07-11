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

        # give the deal full terms so the model produces exit + sensitivity
        c.post(f"/deal/{deal_id}", data={"name": "Test Tower", "purchase_price": "12000000",
               "cap_rate": "6.0", "ltv": "65", "interest_rate": "6.5"}, follow_redirects=True)
        # underwriting model: preview partial (metrics + exit/IRR + sensitivity grid)
        uw = c.get(f"/deal/{deal_id}/underwrite")
        assert uw.status_code == 200 and "DSCR" in uw.text
        assert "Levered IRR" in uw.text and "Equity multiple" in uw.text, "exit analysis missing"
        assert "DSCR sensitivity" in uw.text, "sensitivity grid missing"
        # exit cap knob flows through
        assert c.get(f"/deal/{deal_id}/underwrite?exitcap=7.5").status_code == 200
        # a real .xlsx download (now with exit + grid sheets)
        xlsx = c.get(f"/deal/{deal_id}/model.xlsx")
        assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK", "xlsx not a ZIP"
        assert "spreadsheetml" in xlsx.headers["content-type"]

        # Market page renders seeded base rates + comps; comps import works
        mkt = c.get("/market")
        assert mkt.status_code == 200 and "Base rates" in mkt.text and "Prime" in mkt.text

        # comparable loans for the deal (seeded comps include Multifamily/TX)
        cm = c.get(f"/deal/{deal_id}/comps")
        assert cm.status_code == 200 and "Comparable loans" in cm.text
        # comp filter partial
        assert c.get("/market/comps?asset=Multifamily").status_code == 200
        assert c.get("/market/comps?state=TX&q=agency").status_code == 200
        # inline base-rate edit
        rid = db.list_base_rates()[0]["id"]
        re_ = c.post(f"/market/rate/{rid}", data={"value": "7.10"})
        assert re_.status_code == 200
        assert any(r["id"] == rid and r["value"] == 7.10 for r in db.list_base_rates())
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

        # ── placements: shop the deal to a lender, change status, delete ──
        p = c.post(f"/deal/{deal_id}/placement",
                   data={"lender_name": "Shop Bank", "loan_amount": "9000000", "status": "quoted"})
        assert p.status_code == 200 and "Shop Bank" in p.text
        plc = db.list_placements(deal_id)
        assert plc and plc[0]["status"] == "quoted", plc
        pid = plc[0]["id"]
        c.post(f"/placement/{pid}/status", data={"status": "selected"})
        assert db.get_placement(pid)["status"] == "selected"
        assert c.request("DELETE", f"/placement/{pid}").status_code == 200
        assert not db.list_placements(deal_id)

        # ── files vault: the upload stored the original bytes; serve them back ──
        docs = db.list_documents(deal_id)
        assert docs and docs[0]["size"], "document should have stored bytes now"
        dl = c.get(f"/document/{docs[0]['id']}")
        assert dl.status_code == 200 and b"9,000,000" in dl.content, "vault must serve original bytes"
        assert c.request("DELETE", f"/document/{docs[0]['id']}").status_code == 200

        # ── CSV import: deals + contacts (parity with lender importer) ──
        deal_csv = b"name,pipeline,property_type,city,state,loan_amount\nCSV Deal,acquisition,Retail,Miami,FL,7500000\n"
        di = c.post("/deals/import", files={"file": ("d.csv", deal_csv, "text/csv")}, follow_redirects=True)
        assert di.status_code == 200 and any(d["name"] == "CSV Deal" for d in db.list_deals())
        con_csv = b"name,role,email,company\nSam Sponsor,sponsor,sam@co.com,Sunbelt Holdings\n"
        ci = c.post("/crm/contacts/import", files={"file": ("c.csv", con_csv, "text/csv")}, follow_redirects=True)
        assert ci.status_code == 200
        sam = db.contact_by_email("sam@co.com")
        assert sam and sam["company_id"], "contact import should link/create the company"
        assert any(co["name"] == "Sunbelt Holdings" for co in db.list_companies())

        # a malformed CSV (oversized cell) must NOT 500 the importer
        huge = b"name,notes\nBig Bank," + b"x" * 200000 + b"\n"
        assert c.post("/crm/lenders/import", files={"file": ("l.csv", huge, "text/csv")},
                      follow_redirects=True).status_code == 200

        # ── exports + backup ──
        ex = c.get("/export/deals.csv")
        assert ex.status_code == 200 and "text/csv" in ex.headers["content-type"] and "Test Tower" in ex.text
        assert c.get("/export/lenders.csv").status_code == 200
        assert c.get("/export/contacts.csv").status_code == 200
        bak = c.get("/backup/dealdesk.db")
        assert bak.status_code == 200 and bak.content[:16] == b"SQLite format 3\x00", "backup must be a real sqlite file"

        # ── regression: routes must degrade, not 500, on a bad/missing deal id ──
        assert c.post("/deal/999999/stage", data={"stage": "LOI", "pipeline": "acquisition"}).status_code == 200
        assert "Unknown deal" in c.post("/deal/999999/task", data={"body": "x"}).text
        # a numeric-but-nonexistent deal_id on a contact must not trip the FK
        c.post("/crm/contact", data={"name": "Ghost Ref", "deal_id": "999999"}, follow_redirects=True)
        assert db.list_contacts(), "contact with bad deal_id should still be created (unlinked)"
        # an unknown pipeline must not create a board-invisible deal
        c.post("/deals", data={"name": "Bad Pipe", "pipeline": "zzz"}, follow_redirects=True)
        assert all(d["pipeline"] in ("acquisition", "financing") for d in db.list_deals()), "pipeline must be validated"

        # ── regression: settings test-connection buttons degrade with no key/inbox ──
        assert "No Anthropic key" in c.post("/settings/test/anthropic").text
        assert "No inbox connected" in c.post("/settings/test/email").text

    print("test_smoke OK")


if __name__ == "__main__":
    run()
