"""AI spend meter + monthly cap — a local, own-your-usage take on metered-AI billing.
Each billed call books its REAL cost — computed from the response's token usage against
the model's published per-token price — to the ai_call ledger; a call is refused once
the month's spend has reached MONTHLY_BUDGET_CENTS. If a response carries no usage (a
failure before the reply), a small flat per-feature estimate is booked instead."""
import threading
from contextlib import contextmanager

from .config import settings
from .db import get_conn

# Published $/1M tokens (input, output) per model. Prefix-matched, so a dated or "[1m]"
# variant of a listed id resolves to the same price. Keep in sync with pricing changes.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)   # unknown model → assume Opus-tier so we never UNDER-charge

# Flat fallback estimate, booked only when a response has no usage object (e.g. the call
# raised before returning). ponytail: a coarse floor; the real path above is authoritative.
COSTS = {"agent": 2, "extract": 3, "draft": 1, "match": 2, "docgen": 3}

# ponytail: one global lock serializes check->book so a double-click can't both slip past
# the cap. Fine for a single-user process; the ledger is the backstop.
_LOCK = threading.Lock()


class BudgetExceeded(Exception):
    """Raised when the monthly AI cap has been reached."""


def _price(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in PRICING.items():
        if m.startswith(key):
            return price
    return _DEFAULT_PRICE


def usage_cents(usage, model: str | None) -> int:
    """Real cost, in cents, of one Anthropic response. Cache reads bill at ~0.1× the
    input rate and cache writes at ~1.25×; output at the full output rate. At least 1c
    so a genuine call always registers on the meter."""
    inp_rate, out_rate = _price(model)
    it = getattr(usage, "input_tokens", 0) or 0
    ot = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    dollars = (it + 0.1 * cr + 1.25 * cw) / 1e6 * inp_rate + ot / 1e6 * out_rate
    return max(1, round(dollars * 100))


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


class _Meter:
    """Yielded by charge(). Call `record(resp)` after the API call so the REAL token
    cost is booked; if never recorded, the flat COSTS estimate is used instead."""

    def __init__(self, feature: str):
        self.feature = feature
        self.cents = COSTS.get(feature, 1)   # flat fallback until a response is recorded

    def record(self, resp, model: str | None = None) -> None:
        u = getattr(resp, "usage", None)
        if u is not None:
            self.cents = usage_cents(u, model or settings.llm_model)


@contextmanager
def charge(feature: str):
    """Guard + book one billed AI call. Raises BudgetExceeded (before the network call)
    if the monthly cap is already reached. Yields a meter — record the response so the
    real cost is booked:

        with budget.charge("agent") as meter:
            resp = client.messages.create(...)
            meter.record(resp)

    The cost can only be known AFTER the call, so the guard blocks once spend has reached
    the cap; a single in-flight call may overshoot by its own cost, no more. A raising
    body skips the booking, so only a completed call is billed. The lock is held across
    check->body->book so a double-submit can't both slip past the cap."""
    with _LOCK:
        if spend_this_month() >= settings.monthly_budget_cents:
            raise BudgetExceeded(
                f"Monthly AI cap reached — {remaining_cents()}c left of "
                f"{settings.monthly_budget_cents}c. Raise the cap in Settings to continue.")
        meter = _Meter(feature)
        yield meter
        _record(feature, meter.cents)


def demo() -> None:
    import os
    import tempfile
    settings.db_path = os.path.join(tempfile.mkdtemp(), "b.db")
    from .db import init_db
    init_db()

    # --- real usage-based cost --------------------------------------------------
    class U:  # a stand-in for an Anthropic usage object
        def __init__(self, i, o, cr=0, cw=0):
            self.input_tokens, self.output_tokens = i, o
            self.cache_read_input_tokens, self.cache_creation_input_tokens = cr, cw

    class R:  # a stand-in for a response object (carries .usage, like messages.create)
        def __init__(self, usage):
            self.usage = usage
    # 10k in + 2k out on Opus 4.8 ($5/$25): 10000/1e6*5 + 2000/1e6*25 = 0.05 + 0.05 = $0.10
    assert usage_cents(U(10_000, 2_000), "claude-opus-4-8") == 10, usage_cents(U(10_000, 2_000), "claude-opus-4-8")
    # Haiku is far cheaper for the same tokens
    assert usage_cents(U(10_000, 2_000), "claude-haiku-4-5") < 10
    # cache reads are ~10x cheaper than fresh input
    assert usage_cents(U(0, 0, cr=100_000), "claude-opus-4-8") < usage_cents(U(100_000, 0), "claude-opus-4-8")
    # a tiny call still registers at least 1c; unknown model assumes Opus-tier (not free)
    assert usage_cents(U(1, 1), "claude-opus-4-8") == 1
    assert usage_cents(U(10_000, 2_000), "some-unlisted-model") == 10
    # a meter records the real cost (from a response's .usage) over its flat fallback
    m = _Meter("agent"); assert m.cents == 2
    m.record(R(U(10_000, 2_000)), "claude-opus-4-8"); assert m.cents == 10

    # --- cap enforcement (flat-fallback path when no response is recorded) -------
    settings.monthly_budget_cents = 5
    with charge("agent"):        # 2c flat
        pass
    assert spend_this_month() == 2, spend_this_month()
    with charge("extract"):      # +3c -> 5c, now AT the cap
        pass
    assert spend_this_month() == 5
    try:
        with charge("draft"):    # spend >= cap -> blocked before the call
            raise AssertionError("should not enter")
    except BudgetExceeded:
        pass
    assert spend_this_month() == 5, "at-cap call must not book spend"

    # real cost is booked when recorded
    settings.monthly_budget_cents = 100
    with charge("agent") as meter:
        meter.record(R(U(10_000, 2_000)), "claude-opus-4-8")   # $0.10 = 10c
    assert spend_this_month() == 15, spend_this_month()

    # a raising call books nothing
    try:
        with charge("agent"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert spend_this_month() == 15, "failed call booked spend"
    print("budget.demo (usage-based cost + cap) OK")


if __name__ == "__main__":
    demo()
