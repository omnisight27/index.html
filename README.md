# OmniSight™

## The Intelligence Operating System™

### See It Before It Happens™

**Live site:** https://omnisight27.github.io/index.html

OmniSight is not a dashboard. It is not a collection of tools. It is not
another financial application.

OmniSight is an Intelligence Operating System that helps people,
businesses, nonprofits, investors, researchers, and organizations
understand reality, identify opportunities, forecast outcomes, and make
better decisions before events unfold.

The goal is simple: transform information into understanding, and
understanding into action.

## Vision

OmniSight exists to create understanding. It does not exist to show data.

Most software answers one question: *what happened?* OmniSight answers five,
on every screen, in every engine, in every recommendation:

1. **What happened?**
2. **Why does it matter?**
3. **What happens next?**
4. **What should I do?**
5. **What future does this create?**

## Mission

- Observe reality.
- Remember what matters.
- Understand what changed.
- Forecast what happens next.
- Guide better decisions.
- Help people See It Before It Happens™.

## How it works

```
Reality
    ↓
Observation
    ↓
Event Engine
    ↓
Story Engine
    ↓
NexIntel™ Engine
    ↓
Forecast Engine
    ↓
Video Engine
    ↓
Recommendations
    ↓
Action
```

Each stage, honestly labeled — **Built** means it runs today, **Partial**
means it works but is still proving itself, **Vision** means it is designed
but not built yet:

| Stage | Status | What it is |
|---|---|---|
| Observation | **Built** | Live feeds: OKX, Deribit, DeFiLlama, Finviz, FRED (`Scripts/*_feed.py`). Data lands in `data/latest.json` and `data/intraday.json`. |
| Event Engine | **Vision** | The memory layer: everything important becomes an event — debt paid off, grant awarded, contract won, property purchased, net worth milestone. No general event store exists yet. |
| Story Engine | **Vision** | The understanding layer: every event becomes Yesterday / Today / Tomorrow. No engine yet — the narrative work so far lives in the book and in one-off videos. |
| NexIntel™ | **Built** | The intelligence core. F, the scoring function (`Scripts/decide.py`), turns signals into scored predictions. |
| Forecast Engine | **Built** | F's predictions, graded after the fact by the Proof Book (`proof-book.js`). Horizons from intraday to long-term. |
| Video Engine | **Vision** | Understanding turned into visual stories. Individual videos exist; no engine generates them yet. |
| Recommendations | **Partial** | Daily picks by email, intraday watch alerts, and the F-buy push-through in the gate logic. The system recommends. Nic executes. |
| Action | **Built (human)** | The system never places an order, spends, or touches an account. Every trade is Nic's call. |

## The learning loop — the whole system in six steps

```
Prediction Made
        ↓
Outcome Occurs
        ↓
Compare Result
        ↓
Measure Error
        ↓
Adjust Model Weighting
        ↓
Improve Future Prediction
        ↓
World Events — the cycle starts again, smarter each time
```

The loop already runs. It is F, the correction function, and it corrects
itself — including live weights and gates. Since November 18 it carries a
**promotion gate**: a candidate set of weights goes live only when its
out-of-sample expectancy beats the current live weights on the same test
slice. A candidate that loses stays in shadow and keeps learning. The loop
never ends, and no bad correction can go live twice.

Scoreboard (November 18, 2026 — see `IOS-ARCHITECTURE.md` for the detail):

- Proof Book: **1,582 graded, 35.0% win, paper −$12,072.99.** The system
  is not earning yet.
- The one proven engine: **accumulation setups, 84.6% triggered**
  (35W / 6L). That is the leg earning its weight.
- First live buy: **still uncalled.** Buy bar is 60% (test only), $500
  size, hard stops.

## The platform — what exists today

Every page below is a real page in this repo, live on the site:

| Module | Page | Purpose |
|---|---|---|
| Home | `index.html` | The front door: live picks and the engine's current read. |
| Dashboard | `dashboard.html` | Analytics: live markets, sentiment gauge, Fed commentary, Foresight, Quantum Lab. |
| Picks | `picks.html` | Ranked opportunities — where to focus next. |
| Alerts | `alerts.html` | Critical intelligence requiring action. |
| Portfolio | `portfolio.html` | Everything owned, with live reads. |
| Contract Intel | `govgistics.html` | Government contract opportunity intelligence. |
| Grants | `grants.html` | Funding opportunity intelligence. |
| Economy | `economy.html` | Economic movement: rates, inflation, employment, spending. |
| Land | `land.html` | Property opportunity intelligence. |
| Freedom GP$™ | `debt.html` + `freedom-gps/` | Financial navigation: debt routes, wealth routes, milestones, forecasts. Not budgeting. Navigation. |
| Learn | `learn.html` | Adaptive learning: what to learn right now. |
| Hub | `hub.html` | Command Hub: every account, tool, and resource in one place. |
| Profile | `profile.html` | The personal intelligence model: goals, interests, risk tolerance. |
| Engines | `engines.html` | The engine room. |
| Reports | `reports.html` | The record: what was called, what happened. |

The NexIntel Feed — Critical, Important, Opportunities, Forecasts,
Recommendations — runs through Home, Picks, and Alerts today. The Hub is a
launchpad, not the feed.

## Applications on the OS

```
OmniSight™
The Intelligence Operating System™

├── NexIntel™
│   Intelligence & reasoning core
│
├── Freedom GP$™
│   Financial Intelligence — debt payoff, net worth,
│   cash flow, financial forecasting
│
├── ContractSight™
│   Government opportunity intelligence
│
├── MarketSight™
│   Market & business intelligence
│
├── RiskSight™
│   Threat and risk prediction
│
└── PersonalSight™
    Personal intelligence assistant
```

## The moat

Not AI. Not data. Not dashboards. The moat is:

```
Prediction Accuracy
+
Continuous Learning
+
Relationship Mapping
+
Actionable Recommendations
```

Three of the four are live. **Relationship mapping — the knowledge graph —
is missing**, and it is what the moat is waiting on: without it, the system
scores signals but does not understand what is connected to what. That is
the next build.

## What's missing (in order)

1. **Knowledge graph.** Entities, relationships, events — the layer that
   turns data into understanding.
2. **A durable data loop.** The GitHub Actions schedule is throttled, so
   loop data goes stale. The fix is a reliable schedule, not dependence on
   Actions cron.
3. **A first release.** No versioned release exists. Tag v0.1 when the loop
   is demonstrably improving — not before.
4. **The first proven buy.** One call, made before the event, graded after.

## Long-term vision

OmniSight becomes a Digital Twin of Reality. Not just finances. Not just
business. Life. It remembers decisions, milestones, contracts, properties,
investments, businesses, ancestors, goals, lessons, and opportunities — and
continuously answers the five questions: what happened, why it matters, what
happens next, what should I do, what future does this create.

## Rules

- **Significance is predictability based on variables used for an outcome.**
  A variable that cannot prove predictability gets cut.
- **Proof means A+B=C with a chain that can be explained.** Over 50% is a
  positive signal, under 50% negative, weighted by distance from 50%.
  95% is a read to aim at, never a gate.
- **The edge stays private.** Proof findings, the wave rule, and weights
  are never published.
- **Paper first.** Sims iterate freely, paper only. Live gates change only
  on Nic's explicit yes. The system never trades; Nic executes.
- **One thing at a time.** Build the smallest version that makes one
  prediction, measures whether it was right, and learns from the result.

## The promise

- Observe reality.
- Remember history.
- Create understanding.
- Forecast outcomes.
- Visualize the future.
- Help people See It Before It Happens™.

---

- **Detailed blueprint:** [`IOS-ARCHITECTURE.md`](IOS-ARCHITECTURE.md) —
  the full architecture, stage-by-stage proof, scoreboard, and rules.
- **Earlier scaffold:** [`archive/omnisight-scaffold/`](archive/omnisight-scaffold/) —
  the September prototype, kept verbatim for reference.
- **Brand note:** OmniSight™, NexIntel™, and See It Before It Happens™ are
  brand claims, not confirmed registrations. The IOS™ mark needs attorney
  clearance before it is asserted — it collides with Apple's iOS.
- **Disclaimer:** OmniSight is educational only, not financial advice.
