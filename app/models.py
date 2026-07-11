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


def default_stage(pipeline: str) -> str:
    return PIPELINES.get(pipeline, PIPELINES["acquisition"])[0]


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
