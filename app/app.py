"""FastAPI app: single service, single user. Routes stay thin — data access in
db.py, AI behind ai.py, email behind inbox.py. Auth is one password + a signed
session cookie (Starlette SessionMiddleware). Same shape as OpenProp."""
import secrets

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import ai, budget, db, inbox
from .config import settings
from .models import PIPELINES

app = FastAPI(title="DealDesk")

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


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, settings.dealdesk_password):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
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
    }
    return templates.TemplateResponse("home.html", ctx)


# Feature routes attach to `app` here.
from . import routes_deals     # noqa: E402,F401
from . import routes_crm       # noqa: E402,F401
from . import routes_agent     # noqa: E402,F401
from . import routes_files     # noqa: E402,F401
from . import routes_market    # noqa: E402,F401
from . import routes_inbox     # noqa: E402,F401
from . import routes_settings  # noqa: E402,F401
