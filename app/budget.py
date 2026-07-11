"""AI spend meter + monthly cap — a local, own-your-usage take on metered-AI billing.
Every billed LLM call books a flat per-feature estimate to the ai_call ledger;
a call is refused if it would blow MONTHLY_BUDGET_CENTS. Flat estimates (not real
token accounting) keep it a single knob — tune COSTS if your bills say otherwise."""
import threading
from contextlib import contextmanager

from .config import settings
from .db import get_conn

# ponytail: flat per-call estimate, not real token cost. Upgrade to usage-based
# accounting (read resp.usage) only if the meter drifts far from your Anthropic bill.
COSTS = {"agent": 2, "extract": 3, "draft": 1, "match": 2, "docgen": 3}

# ponytail: one global lock serializes check->spend->record so a double-click can't
# both slip past the cap. Fine for a single-user process; the ledger is the backstop.
_LOCK = threading.Lock()


class BudgetExceeded(Exception):
    """Raised when a billed AI call would exceed the monthly cap."""


def spend_this_month() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_cents), 0) AS c FROM ai_call "
            "WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')").fetchone()
    return row["c"]


def remaining_cents() -> int:
    return settings.monthly_budget_cents - spend_this_month()


def _record(feature: str, cost_cents: int) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO ai_call (feature, cost_cents) VALUES (?, ?)", (feature, cost_cents))


@contextmanager
def charge(feature: str):
    """Reserve budget for one call of `feature`, recording the spend. Raise
    BudgetExceeded (before any network call) if it would blow the cap. Use as:

        with budget.charge("agent"):
            resp = client.messages.create(...)

    `with _LOCK` holds the lock across check -> body -> record and always releases
    it (even if the cap-check DB read raises), so a failure can't wedge the lock.
    A raising body skips _record, so only a completed call is billed."""
    cost = COSTS.get(feature, 1)
    with _LOCK:
        if spend_this_month() + cost > settings.monthly_budget_cents:
            raise BudgetExceeded(
                f"{feature} would cost {cost}c; {remaining_cents()}c left this month. "
                "Raise the monthly cap in Settings to continue.")
        yield
        _record(feature, cost)


def demo() -> None:
    import os, tempfile
    settings.db_path = os.path.join(tempfile.mkdtemp(), "b.db")
    from .db import init_db
    init_db()
    settings.monthly_budget_cents = 5
    with charge("agent"):  # 2c
        pass
    assert spend_this_month() == 2, spend_this_month()
    with charge("extract"):  # +3c -> 5c, exactly at cap
        pass
    assert spend_this_month() == 5
    try:
        with charge("draft"):  # +1c -> over cap
            raise AssertionError("should not enter")
    except BudgetExceeded:
        pass
    assert spend_this_month() == 5, "over-cap call must not book spend"
    # a raising call inside the budget must not book spend (no charge for a failed call)
    settings.monthly_budget_cents = 100
    try:
        with charge("agent"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert spend_this_month() == 5, "failed call booked spend"
    print("budget.demo OK")


if __name__ == "__main__":
    demo()
