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
    company_id    INTEGER REFERENCES company(id) ON DELETE SET NULL,
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
    company_id INTEGER REFERENCES company(id) ON DELETE SET NULL,
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
    data       BLOB,          -- the original bytes, so the vault can serve it back
    mime       TEXT,
    size       INTEGER,
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

-- Email — synced inbound + sent outbound, attached to a deal so each deal has a thread
-- (Lev's per-deal email loop). Inbound is deduped by Message-ID; a NULL deal_id is an
-- email we couldn't match to a deal (still stored, shown in the global activity feed).
CREATE TABLE IF NOT EXISTS email (
    id         INTEGER PRIMARY KEY,
    deal_id    INTEGER REFERENCES deal(id) ON DELETE SET NULL,
    direction  TEXT NOT NULL,          -- 'in' | 'out'
    from_addr  TEXT, to_addr TEXT,
    subject    TEXT, body TEXT,
    message_id TEXT UNIQUE,            -- dedup key for synced inbound (NULL for outbound)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_email_deal ON email(deal_id);

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

-- Placements — which lenders a deal was shopped to and where each stands. A real
-- Lev deal column. UNIQUE(deal_id, lender_name) makes "shop this deal to X"
-- idempotent; lender_name is denormalized so a placement survives deleting the
-- lender from your book (the history of who you called is not the lender's to erase).
CREATE TABLE IF NOT EXISTS placement (
    id          INTEGER PRIMARY KEY,
    deal_id     INTEGER NOT NULL REFERENCES deal(id) ON DELETE CASCADE,
    lender_id   INTEGER REFERENCES lender(id) ON DELETE SET NULL,
    lender_name TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'shopped',  -- see models.PLACEMENT_STATUSES
    loan_amount INTEGER, rate REAL, ltv REAL, term_years REAL, amort_years REAL,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(deal_id, lender_name)
);
CREATE INDEX IF NOT EXISTS idx_placement_deal ON placement(deal_id);
"""

# Columns added after v1 shipped. There is no migration framework and there won't be
# one: SQLite's ALTER TABLE ADD COLUMN is idempotent enough when guarded by
# PRAGMA table_info, and every addition here is nullable-with-default by construction.
# ponytail: append-only column adds. If you ever need to DROP or retype a column,
# that's the point where this earns a real migration tool — not before.
_ADDED_COLUMNS = [
    # (table, column, DDL type) — the file vault keeps the original bytes so a
    # document can be viewed/downloaded, not just re-read as stripped text.
    ("document", "data", "BLOB"),
    ("document", "mime", "TEXT"),
    ("document", "size", "INTEGER"),
    # CRM links: contact -> company, deal -> company (the borrower/sponsor org).
    ("contact", "company_id", "INTEGER REFERENCES company(id) ON DELETE SET NULL"),
    ("deal", "company_id", "INTEGER REFERENCES company(id) ON DELETE SET NULL"),
]


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


def _migrate(conn) -> None:
    """Add any _ADDED_COLUMNS missing from an existing DB. New installs get them from
    SCHEMA's CREATE TABLE; upgrades get them here. Safe to run on every boot."""
    for table, col, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:            # table doesn't exist yet — SCHEMA will create it
            continue
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    # the DB holds the Anthropic key + email App Password in cleartext (settings table).
    # sqlite creates the file 0644 (world-readable) under the default umask; tighten it
    # to owner-only so a second local user can't read the secrets. Best-effort: a
    # filesystem without POSIX perms (some Windows/mounted volumes) just skips it.
    import os
    for path in (settings.db_path, settings.db_path + "-wal", settings.db_path + "-shm"):
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - non-POSIX filesystem
            pass


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# --- deals -------------------------------------------------------------------

_DEAL_COLS = ["name", "pipeline", "stage", "deal_type", "property_type", "address",
              "city", "state", "sponsor", "company_id", "purchase_price", "loan_amount",
              "ltv", "interest_rate", "dscr", "cap_rate", "noi", "lender_name", "status",
              "notes"]


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

_CONTACT_COLS = ("name", "org", "role", "email", "phone", "deal_id", "company_id")


def create_contact(c: dict) -> int:
    cols = ", ".join(_CONTACT_COLS)
    ph = ", ".join(f":{k}" for k in _CONTACT_COLS)
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO contact ({cols}) VALUES ({ph}) RETURNING id",
                           {k: c.get(k) for k in _CONTACT_COLS})
        return cur.fetchone()["id"]


def list_contacts(deal_id: int | None = None, company_id: int | None = None) -> list[dict]:
    """Contacts, joined to their company + deal names so the CRM can show the links."""
    q = ("SELECT c.*, co.name AS company_name, d.name AS deal_name FROM contact c "
         "LEFT JOIN company co ON co.id = c.company_id "
         "LEFT JOIN deal d ON d.id = c.deal_id")
    conds, args = [], []
    if deal_id is not None:
        conds.append("c.deal_id = ?"); args.append(deal_id)
    if company_id is not None:
        conds.append("c.company_id = ?"); args.append(company_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY c.name"
    with get_conn() as conn:
        return _rows(conn.execute(q, tuple(args)))


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

def save_document(deal_id: int | None, filename: str, text: str, terms: dict | None,
                  data: bytes | None = None, mime: str | None = None) -> int:
    """Store a document. `data` is the ORIGINAL bytes — kept so the vault can serve the
    file back (view/download), not just the stripped text. Blobs live in the same
    SQLite file on purpose: one file is the whole workspace, so one backup is complete."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO document (deal_id, filename, text, terms_json, data, mime, size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (deal_id, filename, text, json.dumps(terms) if terms else None,
             data, mime, len(data) if data else None))
        return cur.fetchone()["id"]


def list_documents(deal_id: int) -> list[dict]:
    # never SELECT data here — listing a deal would pull every blob into memory.
    with get_conn() as conn:
        rows = _rows(conn.execute(
            "SELECT id, filename, terms_json, mime, size, created_at FROM document "
            "WHERE deal_id = ? ORDER BY id DESC", (deal_id,)))
    for r in rows:
        r["terms"] = json.loads(r["terms_json"]) if r.get("terms_json") else None
    return rows


def get_document(doc_id: int) -> dict | None:
    """The full row INCLUDING the blob — only for serving one file back."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM document WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def delete_document(doc_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT deal_id FROM document WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM document WHERE id = ?", (doc_id,))
        return row["deal_id"]


def document_texts(deal_id: int, limit: int = 6, chars: int = 4000) -> list[dict]:
    """Extracted text of a deal's documents, for the agent to answer questions over
    (the open version of Lev's per-deal RAG). ponytail: newest-N whole documents,
    truncated — no chunking, no embeddings, no vector store. A single deal's papers fit
    in a modern context window. Add retrieval only when a deal outgrows the window."""
    with get_conn() as conn:
        rows = _rows(conn.execute(
            "SELECT id, filename, substr(text, 1, ?) AS text FROM document "
            "WHERE deal_id = ? AND text IS NOT NULL AND text != '' ORDER BY id DESC LIMIT ?",
            (chars, deal_id, limit)))
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


# --- email (per-deal threads) ------------------------------------------------

def save_email(direction: str, from_addr: str | None, to_addr: str | None, subject: str | None,
               body: str | None, deal_id: int | None = None, message_id: str | None = None) -> int | None:
    """Store one email. Inbound is deduped by message_id — a repeat sync of the same
    message returns the existing row's id (INSERT OR IGNORE, then look it up) rather than
    a duplicate. Returns the row id (existing or new), or None if the insert was ignored
    and no message_id was given to find it by."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO email (deal_id, direction, from_addr, to_addr, subject, "
            "body, message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (deal_id, direction, from_addr, to_addr, subject, body, message_id))
        if cur.rowcount:
            return cur.lastrowid
        if message_id:
            row = conn.execute("SELECT id FROM email WHERE message_id = ?", (message_id,)).fetchone()
            return row["id"] if row else None
        return None


def list_emails(deal_id: int | None = None, limit: int = 50) -> list[dict]:
    q = ("SELECT e.*, d.name AS deal_name FROM email e LEFT JOIN deal d ON d.id = e.deal_id")
    args: tuple = ()
    if deal_id is not None:
        q += " WHERE e.deal_id = ?"
        args = (deal_id,)
    q += " ORDER BY e.id DESC LIMIT ?"
    with get_conn() as conn:
        return _rows(conn.execute(q, args + (limit,)))


def get_email(email_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM email WHERE id = ?", (email_id,)).fetchone()
    return dict(row) if row else None


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
    """Companies with their link counts — a company is only useful in a CRM if you can
    see what hangs off it."""
    with get_conn() as conn:
        return _rows(conn.execute(
            "SELECT co.*, "
            "  (SELECT COUNT(*) FROM contact c WHERE c.company_id = co.id) AS contact_count, "
            "  (SELECT COUNT(*) FROM deal d WHERE d.company_id = co.id) AS deal_count "
            "FROM company co ORDER BY co.name"))


def get_company(company_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def delete_company(company_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM company WHERE id = ?", (company_id,))


# --- placements (which lenders a deal was shopped to) -------------------------

_PLACEMENT_COLS = ["deal_id", "lender_id", "lender_name", "status", "loan_amount",
                   "rate", "ltv", "term_years", "amort_years", "notes"]


def upsert_placement(p: dict) -> int:
    """Shop a deal to a lender (or update that placement). Idempotent per
    (deal_id, lender_name): re-shopping the same lender updates the row in place
    rather than stacking duplicates. Only non-None fields overwrite on conflict, so
    'mark as quoted' doesn't blank out terms captured earlier."""
    row = {k: p.get(k) for k in _PLACEMENT_COLS}
    if not row.get("lender_name"):
        raise ValueError("placement needs a lender_name")
    cols = ", ".join(_PLACEMENT_COLS)
    # status is NOT NULL, so a brand-new row defaults to 'shopped'; but a NULL status on
    # an update must KEEP the stored status, not reset it (re-shopping an already-selected
    # lender must not downgrade it). Every other column: NULL in the new row keeps stored.
    # The UPDATE references the raw :params (not excluded.*), so the INSERT's 'shopped'
    # default can't leak into the update path.
    ph = ", ".join("COALESCE(:status, 'shopped')" if c == "status" else f":{c}"
                   for c in _PLACEMENT_COLS)
    updates = ", ".join(f"{c} = COALESCE(:{c}, placement.{c})"
                        for c in _PLACEMENT_COLS if c not in ("deal_id", "lender_name"))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO placement ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(deal_id, lender_name) DO UPDATE SET {updates}, "
            f"updated_at = datetime('now') RETURNING id", row)
        return cur.fetchone()["id"]


def list_placements(deal_id: int) -> list[dict]:
    with get_conn() as conn:
        return _rows(conn.execute(
            "SELECT * FROM placement WHERE deal_id = ? "
            "ORDER BY CASE status WHEN 'selected' THEN 0 WHEN 'quoted' THEN 1 "
            "WHEN 'shopped' THEN 2 ELSE 3 END, lender_name", (deal_id,)))


def get_placement(placement_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM placement WHERE id = ?", (placement_id,)).fetchone()
    return dict(row) if row else None


def delete_placement(placement_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT deal_id FROM placement WHERE id = ?", (placement_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM placement WHERE id = ?", (placement_id,))
        return row["deal_id"]


# --- dashboard aggregates ----------------------------------------------------

def pipeline_summary() -> list[dict]:
    """Per (pipeline, stage): deal count and total loan $. Aggregated in SQL rather
    than pulled into Python — it's the one query the dashboard hits on every load.
    Only `open` deals: a closed/dead deal is not pipeline."""
    with get_conn() as conn:
        return _rows(conn.execute(
            "SELECT pipeline, stage, COUNT(*) AS deals, "
            "  COALESCE(SUM(loan_amount), 0) AS loan_total, "
            "  COALESCE(SUM(purchase_price), 0) AS price_total "
            "FROM deal WHERE status = 'open' GROUP BY pipeline, stage"))


def deal_totals() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS deals, COALESCE(SUM(loan_amount), 0) AS loan_total, "
            "  COALESCE(SUM(purchase_price), 0) AS price_total "
            "FROM deal WHERE status = 'open'").fetchone()
    return dict(row)


def demo() -> None:
    import os
    import tempfile
    settings.db_path = os.path.join(tempfile.mkdtemp(), "db.db")

    # --- an OLD (v1) DB must survive the upgrade with its rows intact. The v1
    # `deal`/`document` tables have every column EXCEPT the post-v1 additions
    # (deal.company_id, document.data/mime/size) — those are what _migrate adds. -
    with get_conn() as conn:
        conn.executescript(
            "CREATE TABLE deal (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "  pipeline TEXT NOT NULL DEFAULT 'acquisition', stage TEXT NOT NULL, "
            "  deal_type TEXT, property_type TEXT, address TEXT, city TEXT, state TEXT, "
            "  sponsor TEXT, purchase_price INTEGER, loan_amount INTEGER, ltv REAL, "
            "  interest_rate REAL, dscr REAL, cap_rate REAL, noi INTEGER, lender_name TEXT, "
            "  status TEXT DEFAULT 'open', notes TEXT, "
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "  updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
            "CREATE TABLE document (id INTEGER PRIMARY KEY, deal_id INTEGER, "
            "  filename TEXT NOT NULL, text TEXT, terms_json TEXT, "
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')));"
            "INSERT INTO deal (name, stage, loan_amount) VALUES ('Legacy Deal', 'LOI', 1000000);"
            "INSERT INTO document (deal_id, filename, text) VALUES (1, 'old.pdf', 'hi');")
    init_db()   # CREATE TABLE IF NOT EXISTS is a no-op on `deal`; _migrate must ALTER it
    with get_conn() as conn:
        dcols = {r["name"] for r in conn.execute("PRAGMA table_info(document)")}
        assert {"data", "mime", "size"} <= dcols, dcols
        assert "company_id" in {r["name"] for r in conn.execute("PRAGMA table_info(deal)")}
    assert get_deal(1)["name"] == "Legacy Deal", "migration dropped existing rows"
    init_db()   # second boot must be a clean no-op (no duplicate-column error)

    # --- documents keep their original bytes ---------------------------------
    did = create_deal({"name": "Blob Test", "pipeline": "acquisition"})
    doc_id = save_document(did, "ts.pdf", "text", {"ltv": 65}, data=b"%PDF-1.7 body", mime="application/pdf")
    assert get_document(doc_id)["data"] == b"%PDF-1.7 body"
    assert get_document(doc_id)["size"] == 13
    assert "data" not in list_documents(did)[0], "list_documents must not pull blobs"
    assert document_texts(did)[0]["filename"] == "ts.pdf"

    # --- placements are idempotent per (deal, lender) ------------------------
    p1 = upsert_placement({"deal_id": did, "lender_name": "Agency Shop", "rate": 6.5})
    p2 = upsert_placement({"deal_id": did, "lender_name": "Agency Shop", "status": "quoted"})
    assert p1 == p2, "re-shopping the same lender must update, not duplicate"
    plc = list_placements(did)
    assert len(plc) == 1 and plc[0]["status"] == "quoted"
    # the COALESCE upsert must NOT blank the rate captured on the first touch
    assert plc[0]["rate"] == 6.5, plc[0]
    # a status-less re-shop (the hand-add / "+ shop" path) must KEEP the current status,
    # not reset it to 'shopped' — re-shopping a selected lender can't downgrade it.
    upsert_placement({"deal_id": did, "lender_name": "Agency Shop", "status": "selected"})
    upsert_placement({"deal_id": did, "lender_name": "Agency Shop", "rate": 7.0})  # no status
    keep = list_placements(did)[0]
    assert keep["status"] == "selected" and keep["rate"] == 7.0, keep
    upsert_placement({"deal_id": did, "lender_name": "Life Co", "status": "passed"})
    # 'quoted' outranks 'passed' in the list ordering
    assert [p["lender_name"] for p in list_placements(did)] == ["Agency Shop", "Life Co"]
    assert delete_placement(plc[0]["id"]) == did
    assert len(list_placements(did)) == 1

    # --- dashboard aggregates ------------------------------------------------
    update_deal(did, {"loan_amount": 5_000_000, "stage": "LOI"})
    tot = deal_totals()
    assert tot["deals"] == 2 and tot["loan_total"] == 6_000_000, tot   # 1M legacy + 5M
    rows = {(r["pipeline"], r["stage"]): r for r in pipeline_summary()}
    assert rows[("acquisition", "LOI")]["loan_total"] == 6_000_000, rows
    # a closed deal is not pipeline
    update_deal(did, {"status": "closed"})
    assert deal_totals()["loan_total"] == 1_000_000, deal_totals()

    print("db.demo (migration + placements + aggregates) OK")


if __name__ == "__main__":
    demo()
