"""FastAPI app: single service, single user. Routes stay thin — data access in
db.py, AI behind ai.py, email behind inbox.py. Auth is one password + a signed
session cookie (Starlette SessionMiddleware). Same shape as OpenProp."""
import secrets
import time

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import ai, budget, db, inbox
from .config import settings
from .models import PIPELINES, stage_weight

# docs_url/redoc/openapi off: every real route is behind require_auth, but the
# auto-generated schema endpoints are not — and they'd leak the whole route map
# (paths, form fields) to an unauthenticated caller. A single-user app has no use for them.
app = FastAPI(title="DealDesk", docs_url=None, redoc_url=None, openapi_url=None)

_secret = settings.secret_key or secrets.token_hex(32)
if not settings.secret_key:
    print("[dealdesk] no SECRET_KEY set — using an ephemeral one (sessions reset on "
          "restart). Set SECRET_KEY in .env to persist logins.")
if settings.dealdesk_password == "changeme":
    print("[dealdesk] WARNING: DEALDESK_PASSWORD is still 'changeme' — set a real "
          "password in .env before exposing this beyond localhost.")
app.add_middleware(
    SessionMiddleware, secret_key=_secret, same_site="lax",
    https_only=settings.session_https_only)

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["PIPELINES"] = PIPELINES


@app.on_event("startup")
def _startup():
    db.init_db()
    from . import settings_store, seed
    settings_store.load_overrides()  # apply /settings-saved keys over .env
    seed.seed_if_empty()             # sample deals + starter lenders on first run


# --- auth --------------------------------------------------------------------

def require_auth(request: Request):
    if not request.session.get("auth"):
        raise _Redirect("/login")
    return True


class _Redirect(Exception):
    def __init__(self, to: str):
        self.to = to


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.to, status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


# ponytail: in-memory per-IP login throttle — a per-process dict, resets on restart, no
# Redis. A single self-hosted instance only needs to blunt password brute-forcing; scale
# to a shared store only if you run multiple workers behind a load balancer.
_LOGIN_HITS: dict[str, list[float]] = {}
_LOGIN_MAX = 8          # failed attempts per IP
_LOGIN_WINDOW = 300.0   # ...within this many seconds


def _login_blocked(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _LOGIN_HITS.get(ip, ()) if now - t < _LOGIN_WINDOW]
    _LOGIN_HITS[ip] = hits
    return len(hits) >= _LOGIN_MAX


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if _login_blocked(ip):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Too many attempts — wait a few minutes."},
            status_code=429)
    configured = settings.dealdesk_password
    # A blank configured password must NEVER authenticate — compare_digest("", "")
    # is True, so an empty login field would otherwise walk in. Encode to bytes so a
    # non-ASCII password ("Passwörd") doesn't raise TypeError and 500 every attempt.
    if configured and secrets.compare_digest(password.encode("utf-8"), configured.encode("utf-8")):
        _LOGIN_HITS.pop(ip, None)   # clear the counter on a successful login
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    _LOGIN_HITS.setdefault(ip, []).append(time.time())
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Wrong password."}, status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- shared template context -------------------------------------------------

def base_ctx(request: Request) -> dict:
    """Header chrome every page needs: AI spend + capability status."""
    return {
        "request": request,
        "spend_cents": budget.spend_this_month(),
        "budget_cents": settings.monthly_budget_cents,
        "ai_on": ai.available(),
        "email_on": inbox.configured(),
    }


# --- home / dashboard --------------------------------------------------------

def dashboard_stats() -> dict:
    """The numbers behind the dashboard: pipeline $ per pipeline/stage, weighted
    pipeline (loan $ × stage close-probability), deal counts, and the totals. All from
    two aggregate queries + the stage-weight ladder — no per-deal Python."""
    summary = db.pipeline_summary()           # one row per (pipeline, stage), open deals
    totals = db.deal_totals()
    per_pipeline: dict[str, dict] = {}
    weighted_total = 0
    for name in PIPELINES:
        rows = [r for r in summary if r["pipeline"] == name]
        # keep stages in board order; a stage with no open deals still shows as 0.
        # Drop the terminal Closed/Dead stages from the funnel — they aren't live
        # pipeline (a closed deal already left `status='open'`, so its bar is 0 anyway,
        # but the label would still clutter the chart).
        counts = {r["stage"]: r for r in rows}
        live_stages = [s for s in PIPELINES[name] if s not in ("Closed", "Dead")]
        stages = []
        for s in live_stages:
            loan = counts.get(s, {}).get("loan_total", 0)
            w = stage_weight(s)
            stages.append({"stage": s, "deals": counts.get(s, {}).get("deals", 0),
                           "loan_total": loan, "weight": w,
                           "weighted": round(loan * w / 100)})
        p_deals = sum(s["deals"] for s in stages)
        p_loan = sum(s["loan_total"] for s in stages)
        p_weighted = sum(s["weighted"] for s in stages)
        weighted_total += p_weighted
        per_pipeline[name] = {"stages": stages, "deals": p_deals, "loan_total": p_loan,
                              "weighted": p_weighted,
                              # bar-chart scale: the largest stage loan (>=1 avoids /0)
                              "max_loan": max([s["loan_total"] for s in stages] + [1])}
    return {"per_pipeline": per_pipeline, "totals": totals, "weighted_total": weighted_total}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, _=Depends(require_auth)):
    deals = db.list_deals()
    pipelines = {name: sum(1 for d in deals if d["pipeline"] == name) for name in PIPELINES}
    ctx = base_ctx(request)
    ctx |= {
        "deal_count": len(deals),
        "pipelines": pipelines,
        "open_tasks": db.list_tasks(open_only=True)[:8],
        "activity": db.list_activity(limit=15),
        "lender_count": len(db.list_lenders()),
        "stats": dashboard_stats(),
    }
    return templates.TemplateResponse("home.html", ctx)


# Feature routes attach to `app` here.
from . import routes_deals       # noqa: E402,F401
from . import routes_crm         # noqa: E402,F401
from . import routes_agent       # noqa: E402,F401
from . import routes_files       # noqa: E402,F401
from . import routes_market      # noqa: E402,F401
from . import routes_placements  # noqa: E402,F401
from . import routes_inbox       # noqa: E402,F401
from . import routes_settings    # noqa: E402,F401
from . import routes_export      # noqa: E402,F401
