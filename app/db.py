"""SQLite — the whole workspace in one file. Raw stdlib sqlite3, no ORM, no
migrations (same as OpenProp). Deals, contacts, lenders, documents, tasks, an
activity feed, runtime settings, and the AI spend ledger. WAL mode."""
import json
import sqlite3
from contextlib import contextmanager

from .config import settings
from .models import default_stage

SCHEMA = """
CREATE TABLE IF NOT EXISTS deal (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    pipeline      TEXT NOT NULL DEFAULT 'acquisition',
    stage         TEXT NOT NULL,
    deal_type     TEXT,
    property_type TEXT,
    address       TEXT, city TEXT, state TEXT,
    sponsor       TEXT,
    purchase_price INTEGER, loan_amount INTEGER,
    ltv REAL, interest_rate REAL, dscr REAL, cap_rate REAL, noi INTEGER,
    lender_name   TEXT,
    status        TEXT DEFAULT 'open',
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contact (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    org        TEXT,
    role       TEXT,            -- broker | lender | sponsor | attorney | other
    email      TEXT, phone TEXT,
    deal_id    INTEGER REFERENCES deal(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contact_email ON contact(email);

-- Capital-markets side: your own lender book (the open answer to Lev's 7,000
-- lender profiles — you bring/import them, matching.py scores against them).
CREATE TABLE IF NOT EXISTS lender (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    contact_name   TEXT, email TEXT, phone TEXT,
    loan_types     TEXT,   -- JSON list: ["acquisition","bridge","construction",...]
    property_types TEXT,   -- JSON list
    geographies    TEXT,   -- JSON list of 2-letter states, or ["US"] for national
    min_loan       INTEGER, max_loan INTEGER, max_ltv REAL,
    appetite       TEXT DEFAULT 'active',   -- active | selective | paused
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS document (
    id         INTEGER PRIMARY KEY,
    deal_id    INTEGER REFERENCES deal(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    text       TEXT,          -- extracted plaintext (for the agent + re-extraction)
    terms_json TEXT,          -- ExtractedTerms as JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task (
    id         INTEGER PRIMARY KEY,
    deal_id    INTEGER REFERENCES deal(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    due        TEXT,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The realtime feed. Lev pushes this over Pusher; we just append rows and poll.
CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY,
    deal_id    INTEGER REFERENCES deal(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,   -- email_in|email_out|agent|extract|stage|note|system
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_time ON activity(created_at);

-- Runtime config editable from /settings; overrides .env live. Local plaintext.
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- AI spend ledger — one row per billed LLM call, month-summed for the budget cap.
CREATE TABLE IF NOT EXISTS ai_call (
    id         INTEGER PRIMARY KEY,
    feature    TEXT NOT NULL,
    cost_cents INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_month ON ai_call(created_at);

-- Market data (the open version of Lev's capital-markets moat). You bring/maintain
-- it — Lev sells a proprietary feed; here it's your own reference data.
CREATE TABLE IF NOT EXISTS base_rate (        -- loan-pricing benchmarks (Prime, SOFR, Treasuries…)
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    value      REAL,
    delta_1d   REAL,
    delta_1m   REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS loan_comp (        -- closed-loan comps ("Recent terms")
    id            INTEGER PRIMARY KEY,
    asset_type    TEXT, state TEXT, loan_purpose TEXT, capital_provider TEXT,
    ltv REAL, rate REAL, term_years REAL, amort_years REAL, recourse TEXT, issued TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS company (          -- CRM Companies (borrowers, sponsors, lenders as orgs)
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    type       TEXT, location TEXT, website TEXT, notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# --- deals -------------------------------------------------------------------

_DEAL_COLS = ["name", "pipeline", "stage", "deal_type", "property_type", "address",
              "city", "state", "sponsor", "purchase_price", "loan_amount", "ltv",
              "interest_rate", "dscr", "cap_rate", "noi", "lender_name", "status", "notes"]


def create_deal(d: dict) -> int:
    row = {k: d.get(k) for k in _DEAL_COLS}
    row["name"] = (row.get("name") or "Untitled deal").strip()
    row["pipeline"] = row.get("pipeline") or "acquisition"
    row["stage"] = row.get("stage") or default_stage(row["pipeline"])
    row["status"] = row.get("status") or "open"
    cols = ", ".join(_DEAL_COLS)
    ph = ", ".join(f":{c}" for c in _DEAL_COLS)
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO deal ({cols}) VALUES ({ph}) RETURNING id", row)
        return cur.fetchone()["id"]


def update_deal(deal_id: int, fields: dict) -> None:
    """Patch only the columns present in `fields` (skips unknown keys)."""
    cols = [k for k in fields if k in _DEAL_COLS]
    if not cols:
        return
    setclause = ", ".join(f"{c} = :{c}" for c in cols)
    params = {c: fields[c] for c in cols}
    params["id"] = deal_id
    with get_conn() as conn:
        conn.execute(f"UPDATE deal SET {setclause}, updated_at = datetime('now') WHERE id = :id", params)


def move_deal_stage(deal_id: int, stage: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE deal SET stage = ?, updated_at = datetime('now') WHERE id = ?",
                     (stage, deal_id))


def get_deal(deal_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM deal WHERE id = ?", (deal_id,)).fetchone()
    return dict(row) if row else None


def list_deals(pipeline: str | None = None) -> list[dict]:
    q = "SELECT * FROM deal"
    args: tuple = ()
    if pipeline:
        q += " WHERE pipeline = ?"
        args = (pipeline,)
    q += " ORDER BY updated_at DESC"
    with get_conn() as conn:
        return _rows(conn.execute(q, args))


def delete_deal(deal_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM deal WHERE id = ?", (deal_id,))


def deals_context(limit: int = 60) -> list[dict]:
    """Compact snapshot the AI agent reasons over — only the columns worth spending
    tokens on, plus each deal's open-task count."""
    keep = ["id", "name", "pipeline", "stage", "deal_type", "property_type", "city",
            "state", "sponsor", "purchase_price", "loan_amount", "ltv", "interest_rate",
            "dscr", "cap_rate", "lender_name", "status", "updated_at"]
    with get_conn() as conn:
        deals = _rows(conn.execute(
            "SELECT * FROM deal WHERE status != 'closed' ORDER BY updated_at DESC LIMIT ?", (limit,)))
        opencount = {r["deal_id"]: r["n"] for r in conn.execute(
            "SELECT deal_id, COUNT(*) AS n FROM task WHERE done = 0 GROUP BY deal_id")}
    out = []
    for d in deals:
        c = {k: d[k] for k in keep if d.get(k) is not None}
        c["open_tasks"] = opencount.get(d["id"], 0)
        out.append(c)
    return out


# --- contacts ----------------------------------------------------------------

def create_contact(c: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO contact (name, org, role, email, phone, deal_id) "
            "VALUES (:name, :org, :role, :email, :phone, :deal_id) RETURNING id",
            {k: c.get(k) for k in ("name", "org", "role", "email", "phone", "deal_id")})
        return cur.fetchone()["id"]


def list_contacts(deal_id: int | None = None) -> list[dict]:
    with get_conn() as conn:
        if deal_id is None:
            return _rows(conn.execute("SELECT * FROM contact ORDER BY name"))
        return _rows(conn.execute("SELECT * FROM contact WHERE deal_id = ? ORDER BY name", (deal_id,)))


def contact_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM contact WHERE lower(email) = lower(?) LIMIT 1",
                           (email,)).fetchone()
    return dict(row) if row else None


def delete_contact(contact_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM contact WHERE id = ?", (contact_id,))


# --- lenders -----------------------------------------------------------------

_LENDER_JSON = ("loan_types", "property_types", "geographies")
_LENDER_COLS = ["name", "contact_name", "email", "phone", "loan_types", "property_types",
                "geographies", "min_loan", "max_loan", "max_ltv", "appetite", "notes"]


def upsert_lender(l: dict) -> int:
    """Insert or update by unique name. List fields accept a Python list or JSON string."""
    row = {k: l.get(k) for k in _LENDER_COLS}
    for k in _LENDER_JSON:
        if isinstance(row.get(k), (list, tuple)):
            row[k] = json.dumps(list(row[k]))
    row["appetite"] = row.get("appetite") or "active"
    cols = ", ".join(_LENDER_COLS)
    ph = ", ".join(f":{c}" for c in _LENDER_COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _LENDER_COLS if c != "name")
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO lender ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(name) DO UPDATE SET {updates} RETURNING id", row)
        return cur.fetchone()["id"]


def _decode_lender(d: dict) -> dict:
    for k in _LENDER_JSON:
        d[k] = json.loads(d[k]) if d.get(k) else []
    return d


def list_lenders() -> list[dict]:
    with get_conn() as conn:
        return [_decode_lender(dict(r)) for r in conn.execute("SELECT * FROM lender ORDER BY name")]


def get_lender(lender_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM lender WHERE id = ?", (lender_id,)).fetchone()
    return _decode_lender(dict(row)) if row else None


def delete_lender(lender_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM lender WHERE id = ?", (lender_id,))


# --- documents ---------------------------------------------------------------

def save_document(deal_id: int | None, filename: str, text: str, terms: dict | None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO document (deal_id, filename, text, terms_json) VALUES (?, ?, ?, ?) RETURNING id",
            (deal_id, filename, text, json.dumps(terms) if terms else None))
        return cur.fetchone()["id"]


def list_documents(deal_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = _rows(conn.execute(
            "SELECT id, filename, terms_json, created_at FROM document WHERE deal_id = ? "
            "ORDER BY id DESC", (deal_id,)))
    for r in rows:
        r["terms"] = json.loads(r["terms_json"]) if r.get("terms_json") else None
    return rows


# --- tasks -------------------------------------------------------------------

def add_task(deal_id: int, body: str, due: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO task (deal_id, body, due) VALUES (?, ?, ?) RETURNING id",
            (deal_id, body, due or None))
        return cur.fetchone()["id"]


def list_tasks(deal_id: int | None = None, open_only: bool = False) -> list[dict]:
    q = "SELECT t.*, d.name AS deal_name FROM task t JOIN deal d ON d.id = t.deal_id"
    conds, args = [], []
    if deal_id is not None:
        conds.append("t.deal_id = ?"); args.append(deal_id)
    if open_only:
        conds.append("t.done = 0")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY t.done, COALESCE(t.due, '9999'), t.id"
    with get_conn() as conn:
        return _rows(conn.execute(q, tuple(args)))


def set_task_done(task_id: int, done: bool) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT deal_id FROM task WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE task SET done = ? WHERE id = ?", (int(done), task_id))
        return row["deal_id"]


def delete_task(task_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT deal_id FROM task WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM task WHERE id = ?", (task_id,))
        return row["deal_id"]


# --- activity feed -----------------------------------------------------------

def add_activity(kind: str, summary: str, deal_id: int | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO activity (deal_id, kind, summary) VALUES (?, ?, ?) RETURNING id",
            (deal_id, kind, summary[:500]))
        return cur.fetchone()["id"]


def list_activity(limit: int = 30, deal_id: int | None = None) -> list[dict]:
    q = ("SELECT a.*, d.name AS deal_name FROM activity a "
         "LEFT JOIN deal d ON d.id = a.deal_id")
    args: tuple = ()
    if deal_id is not None:
        q += " WHERE a.deal_id = ?"
        args = (deal_id,)
    q += " ORDER BY a.id DESC LIMIT ?"
    with get_conn() as conn:
        return _rows(conn.execute(q, args + (limit,)))


# --- market data (base rates, loan comps) ------------------------------------

def upsert_base_rate(name: str, value, delta_1d=None, delta_1m=None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO base_rate (name, value, delta_1d, delta_1m) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, delta_1d=excluded.delta_1d, "
            "delta_1m=excluded.delta_1m, updated_at=datetime('now')",
            (name, value, delta_1d, delta_1m))


def list_base_rates() -> list[dict]:
    with get_conn() as conn:
        return _rows(conn.execute("SELECT * FROM base_rate ORDER BY name"))


def add_loan_comp(c: dict) -> int:
    cols = ["asset_type", "state", "loan_purpose", "capital_provider", "ltv", "rate",
            "term_years", "amort_years", "recourse", "issued", "notes"]
    row = {k: c.get(k) for k in cols}
    ph = ", ".join(f":{k}" for k in cols)
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO loan_comp ({', '.join(cols)}) VALUES ({ph}) RETURNING id", row)
        return cur.fetchone()["id"]


def list_loan_comps(limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        return _rows(conn.execute("SELECT * FROM loan_comp ORDER BY issued DESC, id DESC LIMIT ?", (limit,)))


def delete_loan_comp(comp_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM loan_comp WHERE id = ?", (comp_id,))


# --- companies (CRM) ---------------------------------------------------------

def upsert_company(c: dict) -> int:
    cols = ["name", "type", "location", "website", "notes"]
    row = {k: c.get(k) for k in cols}
    ph = ", ".join(f":{k}" for k in cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in cols if k != "name")
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO company ({', '.join(cols)}) VALUES ({ph}) "
            f"ON CONFLICT(name) DO UPDATE SET {updates} RETURNING id", row)
        return cur.fetchone()["id"]


def list_companies() -> list[dict]:
    with get_conn() as conn:
        return _rows(conn.execute("SELECT * FROM company ORDER BY name"))


def delete_company(company_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM company WHERE id = ?", (company_id,))
