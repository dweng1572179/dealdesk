# Lev (app.lev.com) — Full Teardown (maximal)

_Hands-on walkthrough of the **unlocked** workspace + live GraphQL/agent-endpoint capture + infrastructure fingerprinting, 2026-07-11._

## 1. What it is

Lev ("Lev Capital", app.lev.com) is **"the AI platform for commercial real estate"** — an AI-agent-driven **deal-management / CRM + capital-markets-data** workspace for CRE sponsors and brokers. You run acquisition and financing deals in it; an AI agent system ("Lev Agent" / internally **Cortex**) reads your deals/files/emails, answers questions, extracts financing terms, builds Excel underwriting models and offering memos, and matches deals to lenders using Lev's proprietary capital-markets database.

**History (why it looks like this):** Lev started as a **CRE debt-financing marketplace** (~$2B/yr loan-origination pipeline by 2022). Rate hikes froze that market, so they **pivoted** the internal deal software into the product — an AI-native CRE CRM + data platform. ~**$200M raised**.

**Account state:** workspace `usc`, **3,000-credit Trial**, gated behind mandatory phone verification (now cleared).

## 2. Navigation & modules

- **Home** — the AI agent launcher ("How can I help you today?") + quick actions (Import data, Generate reports & docs, Capital markets data, Learn about Lev).
- **CRM** → **Contacts**, **Companies**.
- **Market** (capital-markets data — the moat) → **Directory**, **Base rates**, **Recent terms**, **Pulse**.
- **Files** — deal document vault.
- **Pipelines** → **Acquisition** (`/deals/pipeline/19833`), **Financing** (`/deals/pipeline/19834`), **All deals** (`/deals/all`).

## 3. The AI Agent — "Cortex" (multi-agent system)

The centerpiece. Internally namespaced **`/api/cortex/…`**, it's a **multi-agent router** architecture, not a single model. The agent literally says: _"If you share a deal name, address, or a specific task, I'll route it to the right specialist."_

**Observed Cortex agents** (each is a session endpoint `/api/cortex/sessions/me/sessions/{agent}`):
- `copilot-router` — the **router** that dispatches to specialists
- `lev-help-agent` — platform help / support
- `guided-action-agent` — guided actions / onboarding
- `memo-planner-html` — plans offering memoranda (→ HTML)
- `memo-edit-with-ai` — AI editing of memos
- (deal / underwriting / market specialists are implied by the capability list + router behavior)

**Self-described capabilities** (captured from a live agent run):
- **Deal Management** — create & update deals (edit fields, placement/lender notes); **deal vault** (list/read/upload/organize documents); **deal Q&A** (RAG over a deal's docs: property, lease terms, ownership, financials).
- **Market data** — sales comps (sold transactions, cap rates, price/SF), rent comps (unit-level for small multifamily/walk-ups), lender data.
- **Deliverables** — **build Excel underwriting models** (pro forma, DSCR, debt sizing) for any asset class/loan type; **Lev memos / OMs** (full offering memoranda); **Excel edits** (tweak existing models).
- **Platform help** — how-to, escalate issues, create support tickets; **general CRE Q&A** (macro rates, benchmarks, cap-rate trends).
- **Inputs:** `@`-mention deals, files, or teammates for context; a headline flow is _"provide a T-12 + Rent Roll → agent builds an underwriting model."_

The agent streams over the Cortex API (not GraphQL); responses render in a dockable copilot panel. It's the productized version of Lev's claim of being _"trained on millions of financing documents + market transactions, extracting structured data at ~95% accuracy."_

## 4. Deal management / CRM

**Deal object (columns in All deals):** Property, Priority, Type, **Loan request**, Asking price, **Sponsor** (borrower), **Pipelines**, Notes, Date launched, **Placements** (where the deal was shopped to lenders), **Terms** (loan terms), Tasks, Date created, Team members. Deals move through **Acquisition** and **Financing** pipelines. Import (Gmail/Outlook/Excel/GPT/Claude) or create manually. **CRM** = Contacts + Companies. (Trial workspace had 0 deals, so the schema is from the table/GraphQL, not populated rows.)

## 5. Market / capital-markets data (the moat — real counts observed)

- **Directory — 7,287 capital providers/lenders.** Columns: Company, Location, Type (Credit Union, Middle Market Debt Fund, Community Bank, Regional Bank, Life Insurance Company, Small Balance Debt Fund, …), **Similar loans** (to your deals), **12-mo origination volume** ($). Searchable by name + city/state.
- **Base rates — 34 benchmarks.** Prime (6.75%), SOFR (3.53%), Treasury 5/10-Yr, CMT 5/10-Yr, 1-Mo SOFR swaps across tenors, each with 1-day / 1-month deltas. Loan-pricing reference data.
- **Recent terms — 16,133 closed-loan comps.** Asset type, State, Loan request (Permanent/Bridge, Acquisition/Refinance, 1st Mortgage…), Recourse, Capital-provider type, When issued, LTV/LTC, Term/amort, IO, Rate. A debt-market comp set; some rows gated behind an "Unlocked" toggle (credits).
- **Pulse** — market signals / lender-appetite & pricing movement.

This substantiates the "industry's largest source of real-time CRE data" positioning: ~7.3k lenders + ~16k loan comps + live rate feeds, used both by the UI and as tools for the agent (lender matching, comps, pricing).

## 6. API / data model (observed live)

**GraphQL** — all proxied through **`app.lev.com/api/backend/graphql`** (Next.js → backend `api.levcapital.com`). Operations captured:
`AllDealsTableDealsQuery` (first/sort/filters→deals), `AllDealsPipelines`, `AllDealsTotalCountQuery`, `PipelineInsightsTooltipDealCount`, `DealsFilterValuesQuery`, `PropertyTypes` (`borrowerPropertyTypes`), `lenderProfiles`, `lenderTypes`, `Rates`, `SharedTermsQuery`, `SharedTermsCountQuery`, `TermsFilterOptionsQuery`, `ChatTeammates`, `CentralStorageQuery`.

**Cortex agent** — `POST /api/cortex/sessions/me/sessions/{agent}` (copilot-router, lev-help-agent, guided-action-agent, memo-planner-html, memo-edit-with-ai, …); `/api/cortex/import-jobs` (AI import of deals/contacts).

**Other app routes:** `/api/backend/api/v2/push_notifications/auth` (realtime auth), `/api/metronome/available-packages` + `/current-plan` (billing), `/api/posthog/identity` + `/api/surveys` (analytics), `/api/6242990/envelope` (Sentry), `/api/version`.

## 7. Infrastructure (observed)

```
Browser (Next.js SPA @ app.lev.com; no Vercel markers → self-hosted / Cloudflare-fronted)
  │
  ├─ /api/backend/graphql   → GraphQL gateway → backend microservices on *.levcapital.com:
  │      • api.levcapital.com/graphql            (core)
  │      • portfolio-service-production…/graphql (deals/portfolio)
  │      • proxy-server-production…              (proxy / files / signed URLs)
  │      • lev-email-service-production…         (Gmail/Outlook ingest + send, term extraction)
  │
  ├─ /api/cortex/…          → Cortex multi-agent AI platform (router + specialists; server-side LLM + tools)
  │
  Auth:       Auth0
  Realtime:   Pusher (push_notifications/auth) — live activity feed / agent status
  Billing:    Metronome (usage-based credits) + Stripe (payments)
  Analytics:  PostHog (self-proxied via ph.lev.com) + Segment + Google Analytics (G-D85NPLD8N3)
  Errors:     Sentry (project 6242990)
  Maps:       Mapbox + Google Maps/Places
  Marketing:  HubSpot (chat/forms/tracking), Google Ads/DoubleClick, Facebook Pixel, LinkedIn Insight
```

**Notable stack choices:** GraphQL microservices (core + portfolio + email + proxy) on their own `levcapital.com` infra behind a Next.js `/api/backend` proxy; a dedicated **Cortex** agent service with a router→specialist topology; **Metronome** for usage-based credit metering (rare, and telling — they monetize AI/data usage per-unit); Auth0 + mandatory SMS verification for account integrity; Pusher for realtime. The LLM vendor(s) and vector layer are server-side and not exposed client-side.

## 8. Business model

Usage-metered **credits** (3,000 on trial), billed via **Metronome + Stripe**. Gating observed: some **Recent terms** rows are locked behind "Unlocked" (credit spend); the agent + workspace require phone verification. Positioned as a paid B2B SaaS for CRE brokers/sponsors (heavy HubSpot/ads sales motion).

## 9. One-line summary

Lev is a well-funded (~$200M) **AI-native CRE deal platform** — a pivot from its old debt-financing marketplace — built as a Next.js app over a **GraphQL microservices backend** (`*.levcapital.com`: core, portfolio, proxy, email) with a distinct **"Cortex" multi-agent system** (`copilot-router` → specialist agents for deal mgmt, underwriting-model building, memo/OM generation, market data, and help). Its moat is a **proprietary capital-markets dataset** — **7,287 lenders, 16,133 closed-loan comps, and 34 live rate benchmarks** — that both the UI and the agents query to answer deal questions, build Excel models/OMs, and match deals to capital. Auth0 + Pusher + **Metronome** usage-billing + PostHog/Segment/Sentry round out a serious B2B stack.

### Sources
- [Lev — The AI platform for CRE](https://www.lev.com/) · [Lev product system](https://www.lev.com/products) · [Lev AI](https://lev.com/products/lev-ai)
- [CRE Daily — Lev 2026 review](https://www.credaily.com/reviews/lev-review/) · [Crunchbase — Lev](https://www.crunchbase.com/organization/lev)
