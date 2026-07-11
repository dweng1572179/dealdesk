"""The AI layer — the whole pitch ("AI agent over your deals + term extraction +
drafting + lender rationale") on one BYO Anthropic key. Structured features
(term extraction) use messages.parse for schema-validated output; prose features
(agent chat, email/memo drafting, match rationale) use plain messages.

With NO key set, everything degrades to a rules/template fallback so the app is
still useful offline — the agent does keyword deal search, extraction does regex,
drafting uses templates. Same degrade-loudly discipline as OpenProp."""
import json
import logging
import re

from . import budget
from .config import settings
from .models import ExtractedTerms, OMNarrative

log = logging.getLogger("dealdesk")


def available() -> bool:
    return bool(settings.anthropic_api_key)


def _client():
    import anthropic
    # bounded timeout: the SDK default is 10min, so a bad schema/outage would freeze
    # the request instead of degrading to the fallback.
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)


def _text(resp) -> str | None:
    return next((b.text for b in resp.content if b.type == "text"), None)


# --- 1. Agent chat (the centerpiece) ----------------------------------------

_AGENT_SYS = (
    "You are Scout, DealDesk's assistant for a commercial-real-estate dealmaker. You "
    "route a request to the right specialist (deal Q&A, underwriting, lender matching, "
    "memo drafting) and answer. You are "
    "given a JSON snapshot of the user's live deals (id, name, pipeline, stage, "
    "property_type, city/state, prices, loan terms, sponsor, open task count). Answer "
    "the user's question using ONLY that data — do not invent deals, numbers, or "
    "counterparties. Money is in whole dollars, rates/LTV/DSCR as given. When asked to "
    "draft an email or memo, write it ready to send. Be concise and specific; cite deal "
    "names. If the data can't answer the question, say so plainly."
)


def agent_reply(question: str, deals: list[dict]) -> str:
    """Answer a question about the user's deals. Falls back to keyword search."""
    if not available():
        return _agent_rules(question, deals)
    try:
        with budget.charge("agent"):
            resp = _client().messages.create(
                model=settings.llm_model, max_tokens=1200,
                system=_AGENT_SYS,
                messages=[{"role": "user", "content":
                           f"My deals:\n{json.dumps(deals, default=str)}\n\nQuestion: {question}"}])
        return _text(resp) or "(no answer)"
    except budget.BudgetExceeded as e:
        return f"⚠️ {e}"
    except Exception as e:  # noqa: BLE001
        log.warning("agent_reply failed (%s) — falling back to keyword search: %s", type(e).__name__, e)
        return _agent_rules(question, deals)


def _agent_rules(question: str, deals: list[dict]) -> str:
    """No-LLM fallback: surface the deals whose text matches the question's words."""
    if not deals:
        return "You have no open deals yet. Add one from the Deals board to get started."
    words = {w for w in re.findall(r"[a-z0-9]{3,}", question.lower())} - {
        "the", "and", "for", "which", "what", "deal", "deals", "show", "list", "are", "with", "you"}
    scored = []
    for d in deals:
        blob = " ".join(str(v) for v in d.values()).lower()
        hits = sum(1 for w in words if w in blob)
        if hits:
            scored.append((hits, d))
    scored.sort(key=lambda x: -x[0])
    picks = [d for _, d in scored[:5]] or deals[:5]
    lines = [f"• {d['name']} — {d.get('stage','?')} ({d.get('pipeline','?')})"
             f"{' · $' + format(d['loan_amount'], ',') + ' loan' if d.get('loan_amount') else ''}"
             for d in picks]
    header = ("(No Anthropic key set — showing a keyword match instead of a real answer. "
              "Add a key in Settings for the full agent.)")
    return header + "\n\n" + "\n".join(lines)


# --- 2. Term extraction from a document -------------------------------------

def extract_terms(text: str) -> dict:
    """Pull structured deal terms from a term sheet / OM / email. Returns a dict of
    only the fields actually found (sentinels dropped). LLM path is schema-validated;
    fallback is regex over the common money/rate patterns."""
    text = text[:20000]  # cap tokens; a term sheet's numbers live up top
    if not available():
        return _extract_rules(text)
    try:
        with budget.charge("extract"):
            resp = _client().messages.parse(
                model=settings.llm_model, max_tokens=1024,
                system=("Extract commercial-real-estate deal terms from the document. EVERY field "
                        "is required: use \"\" for text and 0 for numbers the document does not "
                        "state. Money in whole dollars, ltv/dscr/cap_rate/interest_rate as plain "
                        "numbers (7.5 not \"7.5%\"). deal_type is one of acquisition/financing/mezz/"
                        "pref. summary is one sentence."),
                messages=[{"role": "user", "content": text}],
                output_format=ExtractedTerms)
        parsed: ExtractedTerms = resp.parsed_output
        out = parsed.to_deal_fields()
        out["summary"] = parsed.summary
        return out
    except budget.BudgetExceeded:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("extract_terms failed (%s) — falling back to regex: %s", type(e).__name__, e)
        return _extract_rules(text)


# number + optional k/m/bn suffix. No 4-digit minimum (that missed "$8.4M").
_NUM_UNIT = r"(\d[\d,]*(?:\.\d+)?)\s*(k|mm|m|bn|b|million|thousand|billion)?"


def _scale(num_str: str, unit: str) -> int:
    n = float(num_str.replace(",", ""))
    unit = (unit or "").lower()
    if unit in ("k", "thousand"):
        n *= 1_000
    elif unit in ("m", "mm", "million"):
        n *= 1_000_000
    elif unit in ("b", "bn", "billion"):
        n *= 1_000_000_000
    return round(n)


def _find_money(t: str, keyword: str, window: int = 80) -> int | None:
    """First MONEY-SHAPED figure within `window` chars after `keyword`. Money-shaped =
    has a $, a thousands comma, or a magnitude suffix (k/m/bn) — that's what stops a
    bare "65" (from "65% LTV, not to exceed $12.5M") being read as a $65 loan, while a
    real "$12,500,000" or "$12.5M" still matches. A figure directly followed by % is a
    percentage, never money, so it's skipped."""
    for km in re.finditer(keyword, t):
        seg = t[km.end():km.end() + window]
        for mm in re.finditer(_NUM_UNIT, seg):
            num_str, unit = mm.group(1), (mm.group(2) or "")
            if seg[mm.end():mm.end() + 1] == "%":       # a percentage, not dollars
                continue
            had_dollar = "$" in seg[max(0, mm.start() - 2):mm.start() + 1]
            if had_dollar or unit or "," in num_str:    # money-shaped
                return _scale(num_str, unit)
    return None


def _extract_rules(text: str) -> dict:
    """Regex fallback — grabs the numbers investors always put in a term sheet. Best
    effort: labeled loudly as approximate. Hardened against the classic false grabs
    (a % read as dollars, a cap rate read as the coupon, an index spread read as the
    all-in rate)."""
    t = text.lower()
    out: dict = {}
    if (v := _find_money(t, r"loan amount")) is not None:
        out["loan_amount"] = v
    if (v := _find_money(t, r"purchase price|acquisition price")) is not None:
        out["purchase_price"] = v
    if m := re.search(r"\bltv\D{0,10}(\d{1,3}(?:\.\d+)?)\s*%?", t):
        out["ltv"] = float(m.group(1))
    if m := re.search(r"\bdscr\D{0,10}(\d(?:\.\d+)?)", t):
        out["dscr"] = float(m.group(1))
    # cap rate first, flexible separator ("cap rate" / "cap-rate" / "cap_rate"), so we
    # can then keep it from being double-counted as the coupon below.
    if m := re.search(r"cap[\s_\-]*rate\D{0,10}(\d{1,2}(?:\.\d+)?)\s*%", t):
        out["cap_rate"] = float(m.group(1))
    # interest rate: prefer an ALL-IN / coupon figure (floating sheets read "SOFR +
    # 3.50% (all-in 8.85%)" — the spread comes first, so keying on 'rate' would grab
    # 3.50). Bare 'rate' is the last resort and never the cap rate we just parsed.
    rate = None
    if m := re.search(r"all[\s\-]?in(?:\s+rate)?\D{0,12}(\d{1,2}(?:\.\d+)?)\s*%", t):
        rate = float(m.group(1))
    elif m := re.search(r"(?:interest rate|coupon|note rate)\D{0,10}(\d{1,2}(?:\.\d+)?)\s*%", t):
        rate = float(m.group(1))
    elif m := re.search(r"\brate\D{0,10}(\d{1,2}(?:\.\d+)?)\s*%", t):
        cand = float(m.group(1))
        if cand != out.get("cap_rate"):   # a bare 'rate' that IS the cap rate is not the coupon
            rate = cand
    if rate is not None:
        out["interest_rate"] = rate
    out["summary"] = "(No Anthropic key — extracted with regex; add a key for full accuracy.)"
    return out


# --- 3. Email / memo drafting -----------------------------------------------

def draft_email(deal: dict, intent: str, to_name: str = "") -> str:
    """Draft an outreach/follow-up email for a deal. Falls back to a template."""
    if not available():
        return _draft_template(deal, intent, to_name)
    try:
        with budget.charge("draft"):
            resp = _client().messages.create(
                model=settings.llm_model, max_tokens=700,
                system=("Draft a concise, professional email for a CRE dealmaker. No pressure, no "
                        "false urgency, no invented facts. Output the email body only (a Subject: "
                        "line then the body). The user sends it themselves."),
                messages=[{"role": "user", "content":
                           f"Deal: {json.dumps(deal, default=str)}\nRecipient: {to_name or 'the counterparty'}\n"
                           f"Purpose: {intent}"}])
        return _text(resp) or _draft_template(deal, intent, to_name)
    except budget.BudgetExceeded as e:
        return f"⚠️ {e}"
    except Exception as e:  # noqa: BLE001
        log.warning("draft_email failed (%s): %s", type(e).__name__, e)
        return _draft_template(deal, intent, to_name)


def _draft_template(deal: dict, intent: str, to_name: str) -> str:
    name = deal.get("name", "the deal")
    who = to_name or "there"
    return (f"Subject: {name} — follow up\n\n"
            f"Hi {who},\n\n"
            f"Following up on {name}"
            f"{' in ' + deal['city'] if deal.get('city') else ''}. {intent}\n\n"
            "Happy to share the full package and jump on a call this week.\n\n"
            "Best,\n")


# --- 4. Lender-match rationale ----------------------------------------------

def match_rationale(deal: dict, lender: dict) -> str | None:
    """One-line pros/cons for a scored lender match. None without a key (the local
    score already stands on its own)."""
    if not available():
        return None
    try:
        with budget.charge("match"):
            resp = _client().messages.create(
                model=settings.llm_model, max_tokens=200,
                system=("In 1-2 sentences, give the pros and cons of taking this CRE deal to this "
                        "lender, based only on the fit between the deal and the lender's stated box."),
                messages=[{"role": "user", "content":
                           f"Deal: {json.dumps(deal, default=str)}\nLender: {json.dumps(lender, default=str)}"}])
        return _text(resp)
    except budget.BudgetExceeded as e:
        # the key IS set — the cap is the reason. Say so, don't imply a missing key.
        return f"⚠️ {e}"
    except Exception as e:  # noqa: BLE001
        log.warning("match_rationale failed: %s", e)
        return None


# --- 5. Deal memo generation ------------------------------------------------

def deal_memo(deal: dict, contacts: list[dict], documents: list[dict]) -> str:
    """Generate a lender-teaser / deal memo (markdown). Falls back to a filled template."""
    if not available():
        return _memo_template(deal)
    try:
        with budget.charge("docgen"):
            resp = _client().messages.create(
                model=settings.llm_model, max_tokens=1500,
                system=("Write a one-page CRE deal memo / lender teaser in Markdown from the deal "
                        "data. Sections: Overview, Property, Financing Request, Sponsor, Key Terms. "
                        "Use only the facts given; write '—' where a fact is missing. No fabrication."),
                messages=[{"role": "user", "content":
                           f"Deal: {json.dumps(deal, default=str)}\n"
                           f"Contacts: {json.dumps(contacts, default=str)}\n"
                           f"Docs on file: {[d.get('filename') for d in documents]}"}])
        return _text(resp) or _memo_template(deal)
    except budget.BudgetExceeded as e:
        return f"> ⚠️ {e}"
    except Exception as e:  # noqa: BLE001
        log.warning("deal_memo failed: %s", e)
        return _memo_template(deal)


# --- 6. Scout agent with tool use (the real router→specialist behavior) -----
# The agent can DO things from chat — create/update deals, run an underwriting model,
# match lenders, draft email, add tasks, and read a deal's documents (RAG). Manual
# tool-use loop (no beta SDK dependency); adaptive thinking so it plans tool calls.
# With no key it degrades to the keyword-search fallback, same as agent_reply.

AGENT_MAX_STEPS = 8   # ponytail: cap the loop; a CRE question rarely needs more turns

_AGENT_TOOLS_SYS = (
    "You are Scout, DealDesk's agent for a commercial-real-estate dealmaker. You can "
    "answer questions AND take actions on the user's workspace with the given tools: "
    "look up deals, create or update a deal, run an underwriting model, match a deal to "
    "the user's lender book, read a deal's uploaded documents, draft an email, and add "
    "tasks. Prefer a tool over guessing — call list_deals to find a deal's id before "
    "acting on it by name. Use ONLY real data from the tools; never invent deals, "
    "numbers, or counterparties. Money is whole dollars. draft_email only DRAFTS — it "
    "does not send; tell the user the draft is ready to review. Be concise and specific, "
    "cite deal names, and confirm what you changed."
)

# ponytail: the deal fields the agent may write. Kept in sync with db._DEAL_COLS minus
# server-managed columns; a tool input outside this set is ignored by db.update_deal.
_AGENT_DEAL_FIELDS = {
    "name": {"type": "string"}, "pipeline": {"type": "string", "enum": ["acquisition", "financing"]},
    "property_type": {"type": "string"}, "city": {"type": "string"}, "state": {"type": "string"},
    "sponsor": {"type": "string"}, "deal_type": {"type": "string"}, "lender_name": {"type": "string"},
    "purchase_price": {"type": "integer"}, "loan_amount": {"type": "integer"}, "noi": {"type": "integer"},
    "ltv": {"type": "number"}, "interest_rate": {"type": "number"}, "dscr": {"type": "number"},
    "cap_rate": {"type": "number"}, "notes": {"type": "string"},
}


def _agent_tools() -> list[dict]:
    return [
        {"name": "list_deals", "description": "List the user's open deals (id, name, pipeline, "
         "stage, type, location, prices, loan terms). Call this first to find a deal's id.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_deal", "description": "Get every field of one deal by id.",
         "input_schema": {"type": "object", "properties": {"deal_id": {"type": "integer"}},
                          "required": ["deal_id"]}},
        {"name": "create_deal", "description": "Create a new deal. Only `name` is required; set "
         "any known fields. pipeline is 'acquisition' or 'financing'.",
         "input_schema": {"type": "object", "properties": _AGENT_DEAL_FIELDS, "required": ["name"]}},
        {"name": "update_deal", "description": "Update fields on an existing deal by id. Only the "
         "fields you pass change.",
         "input_schema": {"type": "object",
                          "properties": {"deal_id": {"type": "integer"}, **_AGENT_DEAL_FIELDS},
                          "required": ["deal_id"]}},
        {"name": "run_underwriting", "description": "Compute the underwriting model for a deal — "
         "DSCR, debt yield, cash-on-cash, and a hold-period IRR + equity multiple. Deterministic "
         "finance math off the deal's fields.",
         "input_schema": {"type": "object", "properties": {
             "deal_id": {"type": "integer"}, "amort_years": {"type": "integer"},
             "hold_years": {"type": "integer"}, "interest_only": {"type": "boolean"}},
             "required": ["deal_id"]}},
        {"name": "match_lenders", "description": "Rank the user's lender book against a deal, with "
         "a fit score and reasons per lender.",
         "input_schema": {"type": "object", "properties": {"deal_id": {"type": "integer"}},
                          "required": ["deal_id"]}},
        {"name": "read_deal_documents", "description": "Read the extracted text of a deal's uploaded "
         "documents (term sheets, OMs, emails) to answer questions about them.",
         "input_schema": {"type": "object", "properties": {"deal_id": {"type": "integer"}},
                          "required": ["deal_id"]}},
        {"name": "draft_email", "description": "Draft (do NOT send) an email for a deal. Returns the "
         "subject and body for the user to review and send.",
         "input_schema": {"type": "object", "properties": {
             "deal_id": {"type": "integer"}, "intent": {"type": "string"},
             "to_name": {"type": "string"}}, "required": ["deal_id", "intent"]}},
        {"name": "add_task", "description": "Add a task to a deal. `due` is an optional YYYY-MM-DD date.",
         "input_schema": {"type": "object", "properties": {
             "deal_id": {"type": "integer"}, "body": {"type": "string"}, "due": {"type": "string"}},
             "required": ["deal_id", "body"]}},
    ]


def run_agent_tool(name: str, inp: dict) -> str:
    """Execute one agent tool against the workspace and return a text result. Pure
    dispatch over db/underwriting/matching — deterministic and testable without a key.
    Any tool error is returned as text (with a marker) so the agent can recover rather
    than the whole turn 500-ing."""
    from . import db, matching, underwriting
    try:
        if name == "list_deals":
            return json.dumps(db.deals_context(), default=str)
        if name == "get_deal":
            d = db.get_deal(int(inp["deal_id"]))
            return json.dumps(d, default=str) if d else "ERROR: no deal with that id."
        if name == "create_deal":
            fields = {k: inp[k] for k in _AGENT_DEAL_FIELDS if k in inp}
            fields["name"] = (inp.get("name") or "Untitled deal")
            if fields.get("pipeline") not in ("acquisition", "financing"):
                fields.pop("pipeline", None)
            did = db.create_deal(fields)
            db.add_activity("agent", f"Scout created deal {fields['name']}", did)
            return f"Created deal id={did} '{fields['name']}'."
        if name == "update_deal":
            did = int(inp["deal_id"])
            if not db.get_deal(did):
                return "ERROR: no deal with that id."
            fields = {k: inp[k] for k in _AGENT_DEAL_FIELDS if k in inp}
            if not fields:
                return "ERROR: no updatable fields given."
            db.update_deal(did, fields)
            db.add_activity("agent", f"Scout updated {', '.join(fields)} on deal {did}", did)
            return f"Updated deal {did}: {', '.join(fields)}."
        if name == "run_underwriting":
            d = db.get_deal(int(inp["deal_id"]))
            if not d:
                return "ERROR: no deal with that id."
            m = underwriting.compute(
                d, amort_years=max(1, int(inp.get("amort_years", 30))),
                hold_years=min(max(1, int(inp.get("hold_years", 5))), 30),
                interest_only=bool(inp.get("interest_only", False)))
            keep = ("purchase_price", "loan_amount", "equity", "annual_debt_service", "dscr",
                    "debt_yield_pct", "cash_on_cash_pct", "returns")
            return json.dumps({k: m[k] for k in keep if k in m}, default=str)
        if name == "match_lenders":
            d = db.get_deal(int(inp["deal_id"]))
            if not d:
                return "ERROR: no deal with that id."
            ranked = matching.rank(d, db.list_lenders())
            if not ranked:
                return "No lenders in the book fit this deal (or the book is empty)."
            return json.dumps([{"name": r["name"], "score": r["score"], "reasons": r["reasons"]}
                               for r in ranked[:8]], default=str)
        if name == "read_deal_documents":
            docs = db.document_texts(int(inp["deal_id"]))
            if not docs:
                return "This deal has no documents with extracted text."
            return json.dumps([{"filename": d["filename"], "text": d["text"]} for d in docs], default=str)
        if name == "draft_email":
            d = db.get_deal(int(inp["deal_id"]))
            if not d:
                return "ERROR: no deal with that id."
            body = draft_email(d, (inp.get("intent") or "Follow up on the deal.").strip(),
                               (inp.get("to_name") or "").strip())
            db.add_activity("agent", f"Scout drafted an email for {d['name']}", d["id"])
            return f"Draft ready (not sent):\n{body}"
        if name == "add_task":
            did = int(inp["deal_id"])
            if not db.get_deal(did):
                return "ERROR: no deal with that id."
            body = (inp.get("body") or "").strip()[:500]
            if not body:
                return "ERROR: task body is empty."
            db.add_task(did, body, (inp.get("due") or "").strip() or None)
            db.add_activity("agent", f"Scout added task to deal {did}: {body[:60]}", did)
            return f"Added task to deal {did}."
        return f"ERROR: unknown tool {name}."
    except budget.BudgetExceeded:
        raise
    except Exception as e:  # noqa: BLE001 — a tool failure is data for the agent, not a 500
        log.warning("agent tool %s failed: %s", name, e)
        return f"ERROR: {type(e).__name__}: {e}"


def agent_act(question: str, deals: list[dict] | None = None) -> str:
    """The tool-using Scout agent. Answers AND acts. Manual tool-use loop; falls back
    to keyword search with no key (same as agent_reply)."""
    from . import db
    if deals is None:
        deals = db.deals_context()
    if not available():
        return _agent_rules(question, deals)
    tools = _agent_tools()
    messages: list = [{"role": "user", "content":
                       f"My current deals (snapshot):\n{json.dumps(deals, default=str)}\n\n{question}"}]
    resp = None
    try:
        client = _client()
        for _ in range(AGENT_MAX_STEPS):
            with budget.charge("agent"):
                resp = client.messages.create(
                    model=settings.llm_model, max_tokens=3072,
                    thinking={"type": "adaptive"},
                    system=_AGENT_TOOLS_SYS, tools=tools, messages=messages)
            # preserve the WHOLE assistant turn (thinking + tool_use blocks) in history
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = run_agent_tool(block.name, block.input or {})
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            messages.append({"role": "user", "content": results})
        return _text(resp) or "(done — no message)"
    except budget.BudgetExceeded as e:
        return f"⚠️ {e}"
    except Exception as e:  # noqa: BLE001
        log.warning("agent_act failed (%s) — falling back to keyword search: %s", type(e).__name__, e)
        return _agent_rules(question, deals)


# --- 7. Offering memorandum narrative -----------------------------------------

def om_narrative(deal: dict, documents: list[dict]) -> dict:
    """Prose sections for an offering memorandum — an executive summary and 3-5
    investment highlights. AI when a key is set; a filled template otherwise. The
    deterministic sections (financials, comps, key terms) are assembled by the caller
    from the underwriting model, so this only writes the narrative. Returns
    {summary: str, highlights: [str, ...]}."""
    if not available():
        return _om_template(deal)
    try:
        with budget.charge("docgen"):
            resp = _client().messages.parse(
                model=settings.llm_model, max_tokens=900,
                system=("Write the narrative for a one-page CRE offering memorandum / lender "
                        "teaser from the deal data. summary is 2-3 sentences of plain prose (no "
                        "markdown, no headers). highlights is 3-5 short investment-highlight "
                        "bullets. Use ONLY the facts given; do not invent numbers or names."),
                messages=[{"role": "user", "content": json.dumps(deal, default=str)}],
                output_format=OMNarrative)
        parsed: OMNarrative = resp.parsed_output
        return {"summary": parsed.summary or _om_template(deal)["summary"],
                "highlights": [h for h in parsed.highlights if h.strip()] or _om_template(deal)["highlights"]}
    except budget.BudgetExceeded as e:
        return {"summary": f"⚠️ {e}", "highlights": []}
    except Exception as e:  # noqa: BLE001
        log.warning("om_narrative failed (%s): %s", type(e).__name__, e)
        return _om_template(deal)


def _om_template(deal: dict) -> dict:
    """No-key OM narrative: assembled from the deal's own fields."""
    name = deal.get("name", "the asset")
    ptype = (deal.get("property_type") or "commercial").lower()
    where = ", ".join(x for x in (deal.get("city"), deal.get("state")) if x)
    price = deal.get("purchase_price")
    summary = (f"{name} is a {ptype} asset"
               f"{' in ' + where if where else ''}"
               f"{' priced at $' + format(price, ',') if price else ''}"
               f"{', sponsored by ' + deal['sponsor'] if deal.get('sponsor') else ''}. "
               "This memorandum summarizes the financing request and key terms for lender review.")
    hl = []
    if deal.get("cap_rate"):
        hl.append(f"{deal['cap_rate']}% going-in cap rate")
    if deal.get("dscr"):
        hl.append(f"{deal['dscr']}x debt-service coverage")
    if deal.get("ltv"):
        hl.append(f"{deal['ltv']}% loan-to-value request")
    if deal.get("property_type"):
        hl.append(f"{deal['property_type']} asset class")
    if not hl:
        hl.append("Add pricing and loan terms to the deal to populate highlights.")
    return {"summary": summary, "highlights": hl}


def _memo_template(deal: dict) -> str:
    def f(k, money=False):
        v = deal.get(k)
        if v is None or v == "":
            return "—"
        return f"${v:,}" if money else str(v)
    return (f"# {deal.get('name','Deal memo')}\n\n"
            f"**Type:** {f('deal_type')} · {f('property_type')}  \n"
            f"**Location:** {f('city')}, {f('state')}  \n"
            f"**Sponsor:** {f('sponsor')}\n\n"
            f"## Financing request\n\n"
            f"- Purchase price: {f('purchase_price', money=True)}\n"
            f"- Loan amount: {f('loan_amount', money=True)}\n"
            f"- LTV: {f('ltv')}%  ·  DSCR: {f('dscr')}  ·  Cap rate: {f('cap_rate')}%\n\n"
            f"_(Generated from deal data with no AI key — add an Anthropic key in Settings "
            f"for a written memo.)_\n")


def demo() -> None:
    # rules fallbacks work with no key
    terms = _extract_rules("Loan Amount: $12,500,000. LTV 65%. DSCR 1.25x. Interest rate 7.5%. Cap rate 5.8%.")
    assert terms.get("loan_amount") == 12_500_000, terms
    assert terms.get("ltv") == 65.0 and terms.get("dscr") == 1.25, terms
    assert terms.get("interest_rate") == 7.5 and terms.get("cap_rate") == 5.8, terms
    terms2 = _extract_rules("Purchase price of $8.4M for the multifamily asset.")
    assert terms2.get("purchase_price") == 8_400_000, terms2

    # cap-rate-first ordering must NOT leak the cap rate into interest_rate
    t3 = _extract_rules("Cap rate 5.5%. Interest rate 7.25%. Loan Amount $20,000,000")
    assert t3.get("interest_rate") == 7.25 and t3.get("cap_rate") == 5.5, t3

    # a % right after the keyword must NOT be read as a dollar figure; the real
    # money-shaped number ($12.5M) is the loan.
    t4 = _extract_rules("LOAN AMOUNT: 65% of appraised value, not to exceed $12,500,000")
    assert t4.get("loan_amount") == 12_500_000, t4
    # cap rate with a non-space separator must not bleed into interest_rate
    t5 = _extract_rules("Cap-Rate: 5.5%. Loan amount $10,000,000")
    assert t5.get("cap_rate") == 5.5 and "interest_rate" not in t5, t5
    # floating-rate sheet: take the all-in coupon, not the index spread
    t6 = _extract_rules("Interest Rate: SOFR + 3.50% (all-in 8.85%). Loan amount $30,000,000")
    assert t6.get("interest_rate") == 8.85, t6
    # a bare number with no money shape is not a loan amount
    t7 = _extract_rules("Loan amount to be determined; target 70% LTV")
    assert "loan_amount" not in t7, t7

    ans = _agent_rules("which office deals in dallas", [
        {"name": "Main St Tower", "stage": "LOI", "pipeline": "acquisition",
         "property_type": "Office", "city": "Dallas", "loan_amount": 5_000_000}])
    assert "Main St Tower" in ans, ans

    # extraction schema must stay lean (all-required sentinels; see ExtractedTerms docstring)
    assert len(ExtractedTerms.model_fields) <= 16, len(ExtractedTerms.model_fields)
    assert "$12,500,000" in _memo_template({"name": "X", "loan_amount": 12_500_000})

    # agent falls back to keyword search with no key (the loop is only entered with a key)
    assert not available(), "demo runs keyless"
    assert "Main St Tower" in agent_act("which office deals in dallas", [
        {"name": "Main St Tower", "stage": "LOI", "pipeline": "acquisition",
         "property_type": "Office", "city": "Dallas"}])

    # --- agent tool executors are deterministic + testable without a key --------
    import os as _os, tempfile as _tempfile
    from . import db as _db
    settings.db_path = _os.path.join(_tempfile.mkdtemp(), "agent.db")
    _db.init_db()
    out = run_agent_tool("create_deal", {"name": "Tower A", "pipeline": "acquisition",
                                         "property_type": "Office", "purchase_price": 10_000_000,
                                         "cap_rate": 6.0, "ltv": 65.0, "interest_rate": 6.5})
    assert "Created deal id=" in out, out
    did = _db.list_deals()[0]["id"]
    assert "Office" in run_agent_tool("get_deal", {"deal_id": did})
    assert "Updated deal" in run_agent_tool("update_deal", {"deal_id": did, "loan_amount": 6_500_000})
    assert _db.get_deal(did)["loan_amount"] == 6_500_000
    uw = json.loads(run_agent_tool("run_underwriting", {"deal_id": did}))
    assert uw.get("dscr") is not None and "returns" in uw, uw
    assert "Added task" in run_agent_tool("add_task", {"deal_id": did, "body": "Call broker"})
    assert _db.list_tasks(did), "agent add_task did not persist"
    assert "no deal with that id" in run_agent_tool("update_deal", {"deal_id": 99999, "notes": "x"})
    assert "unknown tool" in run_agent_tool("bogus", {})

    # OM narrative template (no key) fills from the deal's own fields
    om = _om_template({"name": "Harbor Pointe", "property_type": "Multifamily", "city": "Austin",
                       "state": "TX", "cap_rate": 5.2, "dscr": 1.28, "ltv": 68.0})
    assert "Harbor Pointe" in om["summary"] and "Austin" in om["summary"], om
    assert any("5.2% going-in cap" in h for h in om["highlights"]), om["highlights"]
    print("ai.demo (rules fallback + agent tools + OM) OK")


if __name__ == "__main__":
    demo()
