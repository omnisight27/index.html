# OmniSight — Intelligence Operating System (IOS) Architecture

## Vision

OmniSight™
The Intelligence Operating System™
See It Before It Happens™

OmniSight™ is an Intelligence Operating System that continuously gathers, connects, scores, and learns from information across global data sources to help people and organizations anticipate events, evaluate opportunities, reduce risk, and take informed action.

By combining artificial intelligence, predictive modeling, economic analysis, behavioral science, knowledge mapping, and adaptive learning, OmniSight transforms information into intelligence and intelligence into action.

OmniSight is a continuously learning Intelligence Operating System that transforms information into understanding, understanding into prediction, and prediction into action. It works across digital, economic, governmental, commercial, and human systems, identifying patterns, relationships, risks, and opportunities before they become obvious.

Unlike traditional software that reports what has already happened, OmniSight is designed to understand what is happening now, forecast what is likely to happen next, and recommend actions that improve outcomes.

OmniSight is not a search engine, dashboard, or chatbot. It is an Intelligence Operating System for the future of decision-making.

Every signal is scored.

Every prediction is measured.

Every outcome becomes a learning opportunity.

OmniSight™ exists to help humanity make better decisions by turning global information into practical intelligence.

OmniSight's mission is simple:

See It Before It Happens™

## The learning loop (the whole system in six steps)

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

The loop never ends. Each pass feeds back into the world it observes.

In short:

```
Prediction
   ↓
Outcome
   ↓
Error
   ↓
Adjustment
   ↓
Better Prediction
```

Without this loop, OmniSight is another analytics dashboard. With it, it is a
living intelligence system. This loop already runs: it is F, the correction
function (`Scripts/decide.py`, weights in `data/corrected_weights.json`).

## The full pipeline (world → action → learning)

```
WORLD EVENTS
     ↓
DATA COLLECTION
     ↓
KNOWLEDGE GRAPH
     ↓
PATTERN RECOGNITION
     ↓
PREDICTIONS
     ↓
CONFIDENCE SCORING
     ↓
RECOMMENDED ACTIONS
     ↓
OUTCOMES
     ↓
SELF-CORRECTION
     ↓
IMPROVED INTELLIGENCE
     ↓
WORLD EVENTS (again — the pipeline is a loop, not a line)
```

## The layers

```
GLOBAL DATA LAYER
────────────────────

News
Markets
Government
Economics
Crypto
Business
Social Signals

        ↓

KNOWLEDGE LAYER
────────────────────

Entities
Relationships
Events
Trends
Context

        ↓

INTELLIGENCE LAYER
────────────────────

Scoring
Forecasting
Correlation
Risk Analysis
Opportunity Analysis

        ↓

LEARNING LAYER
────────────────────

Predictions
Outcomes
Error Tracking
Weight Adjustments
Confidence Calibration

        ↓

ACTION LAYER
────────────────────

Recommendations
Automation
Alerts
Tasks
Execution

        ↓

PERSONAL LAYER
────────────────────

Goals
Preferences
Memory
Behavior
Adaptation
```

Status: Data, Intelligence, Learning and Action layers are live; Knowledge is
missing; Personal is partial (profile, preferences, Money GPS goals). See the
stage table below for the file-by-file proof.

## Each stage, mapped to what actually exists (verified 2026-11-18)

| Stage | Status | Where it lives |
|---|---|---|
| World events / data collection | **Built** | Live feeds: OKX, Deribit, DeFiLlama, Finviz, FRED (`Scripts/*_feed.py`, `Scripts/econ_features.py`). Data lands in `data/latest.json` and `data/intraday.json`. |
| Knowledge graph | **Missing** | No graph code exists. Only passing mentions in data snapshots. The old scaffold (now in `archive/omnisight-scaffold/`) has stub models for person / organization / relationship — not wired to anything. This is the biggest gap. |
| Pattern recognition | **Built (partial)** | Engines in `Scripts/nexintel.py`; pattern, cycle and CMI grading tests. Precedent model (`Scripts/precedent.py`) is display-only. |
| Predictions | **Built** | F, the scoring function (`Scripts/decide.py`). Picks written to `data/latest.json`. |
| Confidence scoring | **Built (partial)** | Corrected weights (`data/corrected_weights.json`, Nov 11). Bars read raw confidence; calibrated display only. |
| Recommended actions | **Built (partial)** | Daily picks email + intraday watch alerts (see `.github/workflows/`). F-buy push-through is live in the gate logic (`Scripts/gate_log.py`, `Scripts/nexintel.py`). |
| Outcomes | **Built** | The Proof Book (`proof-book.js` + grading tests). Every prediction is graded against what actually happened. |
| Self-correction | **Built** | The F correction loop runs on its own, including live weights and gates (Nic's Nov 11 decision). Sims (`Scripts/sim_picks.py`) grade variants paper-only. |
| Improved intelligence | **In progress** | The loop runs. The proof is the trend, not one number. See scoreboard. |

## Scoreboard (Nov 18, 2026)

- Proof Book: **1,582 graded, 35.0% win, paper −$12,072.99.** The system is
  not earning yet.
- History win rate: **49.4%** (843W / 865L, n = 1,708).
- The one proven engine: **accumulation setups, 84.6% triggered** (35W / 6L).
  That is the leg earning its weight. Everything else is still proving itself.
- First live buy: **still uncalled.** Buy bar is 60% (test only), $500 size,
  hard stops. **The system never places an order. Nic executes.**

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

Each part, honestly:

- **Prediction Accuracy** — the Proof Book. 35.0% overall, 84.6% on
  accumulation setups. The number is the moat, and it has to climb.
- **Continuous Learning** — the F correction loop. Built and running.
- **Relationship Mapping** — the knowledge graph. **Missing.** This is the
  piece the moat is waiting on: without it, the system scores signals but
  does not understand what is connected to what.
- **Actionable Recommendations** — picks, alerts, F-buy push-through. Built;
  the system recommends, Nic executes.

Three of the four are live. Relationship mapping is what turns the other
three into something nobody else has.

## What is missing (in order)

1. **Knowledge graph.** Entities, relationships, events — the layer that turns
   data into understanding. Without it, pattern recognition works on signals
   alone, not on what is connected to what.
2. **A durable data loop.** The GitHub Actions schedule is throttled (hourly
   runs collapsing to 2–5 a day); loop data goes stale. Fix: run the engine
   from a reliable schedule instead of depending on Actions cron.
3. **A first release.** No versioned release exists. Tag v0.1 when the loop
   above is demonstrably improving — not before.
4. **The first proven buy.** One call, made before the event, graded after.

## Rules

- **Significance is predictability based on variables used for an outcome.**
  A variable that cannot prove predictability gets cut.
- **Proof means A+B=C with a chain that can be explained.** Over 50% is a
  positive signal, under 50% negative, weighted by distance from 50%.
  95% is a read to aim at, never a gate.
- **The edge stays private.** Proof findings, the wave rule, and weights are
  never published.
- **Paper first.** Sims iterate freely, paper only. Live gates change only on
  Nic's explicit yes. The system never trades; Nic executes.
- **One thing at a time.** Build the smallest version that makes one
  prediction, measures whether it was right, and learns from the result.
  Everything else is expansion, not invention.

## Brand

- **OmniSight™** — the product. The Intelligence Operating System.
- **See It Before It Happens™** — the promise.
- **NexIntel™** — the reasoning and intelligence engine.

Applications on the OS:

```
OmniSight™
The Intelligence Operating System™

├── NexIntel™
│   Intelligence & reasoning core
│
├── Freedom GP$™
│   Financial Intelligence
│   Debt payoff
│   Net worth
│   Cash flow
│   Financial forecasting
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

- Note: the **IOS™** mark needs attorney clearance before it is asserted —
  it collides with Apple's iOS. Trademark registrations have not been
  verified; ™ here is a brand claim, not a confirmed registration.
