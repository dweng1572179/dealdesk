"""The AI layer — Lev's whole pitch ("AI agent over your deals + term extraction +
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
from .models import ExtractedTerms

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
    "You are Cortex, DealDesk's assistant for a commercial-real-estate dealmaker. You "
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


# number + optional k/m/bn suffix. No 4-digit minimum (that missed "$8.4M"); every
# use is anchored to a keyword ("loan amount", "purchase price"), so short raw
# numbers can't be grabbed out of context.
_MONEY = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|mm|m|bn|b|million|thousand|billion)?\b"


def _money(m) -> int:
    n = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit in ("k", "thousand"):
        n *= 1_000
    elif unit in ("m", "mm", "million"):
        n *= 1_000_000
    elif unit in ("b", "bn", "billion"):
        n *= 1_000_000_000
    return round(n)


def _extract_rules(text: str) -> dict:
    """Regex fallback — grabs the numbers investors always put in a term sheet."""
    t = text.lower()
    out: dict = {}
    if m := re.search(r"loan amount\D{0,12}" + _MONEY, t):
        out["loan_amount"] = _money(m)
    if m := re.search(r"(?:purchase price|acquisition price)\D{0,12}" + _MONEY, t):
        out["purchase_price"] = _money(m)
    if m := re.search(r"\bltv\D{0,10}(\d{1,3}(?:\.\d+)?)\s*%?", t):
        out["ltv"] = float(m.group(1))
    if m := re.search(r"\bdscr\D{0,10}(\d(?:\.\d+)?)", t):
        out["dscr"] = float(m.group(1))
    # (?<!cap ) so "cap rate 5.5%" can't satisfy the bare "rate" alternative and
    # bleed the cap rate into interest_rate when it appears first in the doc.
    if m := re.search(r"(?:interest rate|coupon|(?<!cap )rate)\D{0,10}(\d{1,2}(?:\.\d+)?)\s*%", t):
        out["interest_rate"] = float(m.group(1))
    if m := re.search(r"cap rate\D{0,10}(\d{1,2}(?:\.\d+)?)\s*%", t):
        out["cap_rate"] = float(m.group(1))
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

    ans = _agent_rules("which office deals in dallas", [
        {"name": "Main St Tower", "stage": "LOI", "pipeline": "acquisition",
         "property_type": "Office", "city": "Dallas", "loan_amount": 5_000_000}])
    assert "Main St Tower" in ans, ans

    # extraction schema must stay lean (all-required sentinels; see ExtractedTerms docstring)
    assert len(ExtractedTerms.model_fields) <= 16, len(ExtractedTerms.model_fields)
    assert "$12,500,000" in _memo_template({"name": "X", "loan_amount": 12_500_000})
    print("ai.demo (rules fallback) OK")


if __name__ == "__main__":
    demo()
