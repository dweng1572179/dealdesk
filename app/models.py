"""Domain vocabulary + the AI-boundary schemas. Deals/contacts/lenders live as
plain dicts in db.py (raw sqlite, no ORM — same call as OpenProp); the pydantic
models here are only where a shape must be validated: the LLM's term-extraction
output and the structured agent bits."""
from __future__ import annotations

from pydantic import BaseModel

# Pipelines and their ordered stages. The board renders one column per stage.
# ponytail: hardcoded — two pipelines, fixed stages. Make it a DB table only if a
# user ever needs custom pipelines (they won't for a single-desk tool).
PIPELINES: dict[str, list[str]] = {
    "acquisition": ["Sourcing", "Underwriting", "LOI", "Under Contract",
                    "Due Diligence", "Closing", "Closed", "Dead"],
    "financing": ["Intake", "Packaging", "In Market", "Term Sheets",
                  "Selected", "Closing", "Closed", "Dead"],
}
DEAL_TYPES = ["acquisition", "financing", "mezz", "pref"]
PROPERTY_TYPES = ["Multifamily", "Office", "Industrial", "Retail", "Mixed-Use",
                  "Hospitality", "Land", "Other"]
LENDER_APPETITE = ["active", "selective", "paused"]

# Where a deal stands with one lender you took it to (the CRM "Placements" column).
PLACEMENT_STATUSES = ["shopped", "quoted", "passed", "selected", "dead"]

# Probability a deal at this stage actually closes — the multiplier behind "weighted
# pipeline". ponytail: a hardcoded heuristic ladder, not a learned model. It's the
# standard CRM convention and there is no close-history to fit against on day one;
# make it per-user config once someone's own hit-rate disagrees with it.
STAGE_WEIGHTS: dict[str, int] = {
    # acquisition
    "Sourcing": 10, "Underwriting": 20, "LOI": 40, "Under Contract": 60,
    "Due Diligence": 75,
    # financing
    "Intake": 10, "Packaging": 25, "In Market": 40, "Term Sheets": 60, "Selected": 80,
    # shared tail
    "Closing": 90, "Closed": 100, "Dead": 0,
}


def default_stage(pipeline: str) -> str:
    return PIPELINES.get(pipeline, PIPELINES["acquisition"])[0]


def stage_weight(stage: str) -> int:
    """Close probability (0-100) for a stage. Unknown stage -> 50 (no information is
    not the same as no chance)."""
    return STAGE_WEIGHTS.get(stage, 50)


class ExtractedTerms(BaseModel):
    """LLM term-sheet extraction. EVERY field is REQUIRED with a sentinel ("" / 0),
    never `| None` and never a default — that combination is what keeps Anthropic
    structured outputs fast and un-400'd (see OpenProp's FilterExtract: nullable
    unions cap at 16 and hard-400; optional fields make the grammar hang for >75s).
    15 required scalar fields, one shape, ~5s."""
    property_name: str
    address: str
    city: str
    state: str
    property_type: str
    deal_type: str
    purchase_price: int
    loan_amount: int
    ltv: float
    interest_rate: float
    dscr: float
    cap_rate: float
    sponsor: str
    lender_name: str
    summary: str

    def to_deal_fields(self) -> dict:
        """Sentinels -> omitted, so unmentioned terms don't overwrite real deal data."""
        out: dict = {}
        for k, v in self.model_dump().items():
            if v in ("", 0, 0.0):
                continue
            out[k] = v
        # `summary` isn't a deal column — it rides along for the activity note only.
        out.pop("summary", None)
        return out


class OMNarrative(BaseModel):
    """Prose sections for the offering-memorandum generator. Two required fields, one a
    list of short strings — well within the structured-output limits (see ExtractedTerms
    for why the schema stays lean)."""
    summary: str
    highlights: list[str]
