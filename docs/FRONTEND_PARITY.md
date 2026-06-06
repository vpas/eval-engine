# Frontend ↔ UX-prototype parity

> A view-by-view audit of the implemented Next.js dashboard (`frontend/`) against the interactive UX
> prototype (`model-eval-portal/project/portal/*.jsx`, the design source of truth). Each difference is
> classified; the easy/clear ones are fixed in the same pass, the rest are documented with a reason.
>
> Legend: **✅ match** · **🎭 intentional** (deliberate adaptation to the real backend/routing) ·
> **🐞 bug → fixed** · **🕳 gap → fixed** · **🕳 gap → deferred** (needs backend work or is sizable) ·
> **🚫 out-of-scope** (prototype-authoring tooling, or no backend data exists) · **🤔 needs design**.

The governing reason most "differences" exist: the prototype runs on a **mocked `window.DB`** with
fabricated fleet metrics, live sample streams, per-sample rows, and simulated diffs. The real app is
wired to the FastAPI backend, so anything the backend doesn't expose is either adapted, deferred behind
a small backend change, or intentionally dropped rather than fabricated.

---

## Shell / navigation / design system

| Item | Class | Notes |
|---|---|---|
| GitHub-dark design system (`styles.css` → `globals.css`) | ✅ match | ported verbatim |
| **System font vs Archivo** | 🐞 fixed | rewrite had mapped `--sans/--mono` to Archivo/JetBrains; reverted to the prototype's system stacks (commit `22d0f36`) |
| App bar: brand, Dashboard/Training/Compare nav, search, New-run, avatar | ✅ match | nav uses real routes + `usePathname`; detail-page breadcrumb preserved |
| Header search box | 🎭 intentional | decorative in the prototype too (no global search backend); left as a styled no-op |
| Tweaks panel (density/accent/sharp) | 🚫 out-of-scope | prototype-authoring tool; app ships the compact (`dense`) default |
| `variations.jsx`, `design-canvas.jsx`, `tweaks-panel.jsx` | 🚫 out-of-scope | prototype design-exploration scaffolding, not product |

## Dashboard (`app/page.tsx` ↔ `dashboard.jsx`)

| Item | Class | Notes |
|---|---|---|
| Runs table (filters: search/status/eval/mine, selection → Compare) | ✅ match | wired to `GET /runs`, auto-refresh |
| Stat tiles | 🎭 intentional | computed from `/runs` (Active / Avg-accuracy / Samples / Runs). Prototype's "Spend today" + per-tile **sparklines** dropped — `/runs` returns no cost or time-series; not fabricated |
| "Execution plane" / **FleetStrip** (KEDA workers, gateway QPS, provider lanes) | 🕳 deferred | no fleet-metrics endpoint exists; would need a real `/fleet` (Prometheus/KEDA) source. Omitted rather than mocked |
| **`sweep` badge** + **eval `@version`** on rows | 🕳 fixed | `list_runs` now returns `eval_version`/`sweep`; the dashboard shows the eval @version and a "sweep" badge on checkpoint-eval runs |
| Live progress **bar** inside an active row | 🎭 intentional | the list endpoint has no per-row progress; active rows show an "in progress" spinner and link to the run page (which polls real progress) |
| Cost column | 🕳 fixed | `list_runs` now returns `cost_usd`; shown as a Cost column |

## Run detail (`app/runs/[id]/page.tsx` ↔ `run.jsx` + `analyze.jsx`)

| Item | Class | Notes |
|---|---|---|
| Header (id, status, eval@ver, provider, provenance, re-run) | ✅ match | + real `image_digest` / `provider_fingerprint` |
| Live progress (while active) | ✅ match | real `progress` polled from `GET /runs/{id}` |
| **LiveMonitor** (live-accuracy sparkline, throughput, cost/budget, ETA, **live sample feed**, gateway, failures) | 🕳 deferred | entirely simulated in the prototype; no live per-sample stream / gateway-QPS endpoint. The real equivalent (progress bar + live rollup on the run row) is shown. A live feed would need a streaming endpoint |
| Pause / Cancel buttons (active runs) | 🚫 out-of-scope | no cancel/pause API yet |
| Analysis: headline metrics + **CI bar** | ✅ match (fixed) | added the CI bar + `MetricTile` to match (`b1c355f`) |
| Accuracy-by-category bars + **Score-distribution histogram** | ✅ match (fixed) | histogram computed client-side over retained samples |
| **Full-width Sample explorer** + filter bar (search / all·fail·pass / category / min-score) | 🐞 fixed | was a half-width table beside the chart; now the prototype's full-width panel (`3d0f761`) |
| Sample columns: **Tokens / Latency / Error** | 🕳 fixed | `analytics.samples` + `/results` now return `tokens`/`latency_ms`/`error_type`; the Sample explorer shows the columns (`latency_ms` is 0 today — the runner doesn't time samples yet) |
| Non-retained transcripts shown as "**not kept**" | 🕳 fixed | sample-by-default retention keeps all failures + ~25% of passes, so some rows have no transcript; labelled instead of a bare "—" (`b1c355f`) |
| **Agentic trajectory inspector** (`agentic-transcript.jsx`: steps, tool calls, minimap) | 🎭 intentional | the rich trajectory lives in the `.eval` log; the drawer links to the **embedded Inspect viewer** (`/inspect/`) rather than re-implementing it. The drawer renders the single-turn input/output/target/scores we store |

## Launch (`components/launch.tsx` ↔ `launch.jsx`)

| Item | Class | Notes |
|---|---|---|
| Eval picker, dataset slice (full/subset), model(s), epochs, budget, single/matrix | ✅ match | wired to `GET /evals` + `POST /evals/{id}/launch` (matrix = N launches) |
| RunSpec + Estimate panels | 🎭 intentional | RunSpec shows the eval's resolved dataset/harness/scorers; estimate shows runs/slice/epochs/budget (no client-side cost model → no $/token estimate) |
| **seed / temperature / transcript-retention toggle** | 🕳 fixed | `LaunchFromEval` + `/evals/{id}/launch` now accept `temperature`/`seed`/`transcript_sample_rate`; the composer exposes Temperature/Seed inputs + a keep-all toggle |
| **max-inflight** override | 🎭 intentional | the per-run cap is auto-classified by lane (SCHEDULER §2/§3); launch-from-eval doesn't override it |
| Mock-output field (keyless runs) | 🎭 intentional addition | not in the prototype; lets you launch deterministic `mockllm` runs from the UI |

## Training (`app/training/*` ↔ `training.jsx` + `training-chart.jsx` + `training-drill.jsx`)

| Item | Class | Notes |
|---|---|---|
| Trajectory chart (lines, expected band, loss overlay, steps↔tokens, anomaly rings, scrub, now-marker) | ✅ match | ported; wired to `GET /training/{id}/series` + `/checkpoints` |
| Eval×checkpoint heatmap, anomalies panel (+diagnosis), threshold slider | ✅ match | |
| Drill drawer: root-cause/diagnosis, signals grid, category-regression, regressed samples, "Diff in Compare" | ✅ match | uses real anomaly objects from `/anomalies` |
| Checkpoint inspector (movers vs previous) | ✅ match | |
| **Run list page** vs the prototype's **in-header run-picker dropdown** | 🎭 intentional | real routing: a `/training` list + `/training/{id}` detail instead of a single-page dropdown |
| **"Latest checkpoint"** header button | 🕳 fixed | added — opens the checkpoint inspector for the newest checkpoint |
| **Cross-run overlay** (ghost line of another training run) | 🕳 deferred | feasible (fetch a second run's series) but a non-trivial chart addition; deferred |
| Alerts / Export buttons | 🚫 out-of-scope | no alerting/export backend |
| **"Scan now"** button | 🎭 intentional addition | drives the monitor on-demand (the prototype's monitor loop is implicit) |

## Compare (`app/compare/page.tsx` ↔ `compare.jsx` + `samplediff.jsx`)

| Item | Class | Notes |
|---|---|---|
| Run-selection chips + add-run picker, mixed-eval warning | 🕳 fixed | added |
| **Leaderboard** (ranked, CI bars, Δ-vs-best, $/pt) | 🕳 fixed | ported; CI from `accuracy_ci`, $/pt from cost |
| **A/B diff** (verdict, headline metrics side-by-side, diverging category deltas, CI-overlap significance) | 🕳 fixed | ported |
| **Per-sample diff** | 🕳 fixed | the prototype *simulates* per-sample data; the real impl aligns **actual** `analytics.samples` by `sample_id` across runs (pass/fail matrix, disagreement filter) — strictly better than the mock |
| (previous impl) generic metric table | 🐞 replaced | the prior simplified table is superseded by the three prototype modes |

---

## Summary

- **Bugs fixed:** font (Archivo→system), run-page sample-explorer width, "not kept" labelling, Compare reduced to a plain table.
- **Gaps fixed:** full Compare (Leaderboard / A/B / Per-sample with real data), run-page CI bar + histogram, training "Latest checkpoint" button; **and the small-backend-change items** — dashboard `sweep`/`eval@version`/cost columns + sample Tokens/Latency/Error (`list_runs` / `analytics.samples` extended), launch seed/temp/retention knobs (`LaunchFromEval` extended).
- **Still deferred (larger):** cross-run training overlay (chart work); per-sample latency (the runner doesn't time individual samples yet, so `latency_ms`=0).
- **Out-of-scope / intentional (no backend data, or prototype tooling):** FleetStrip, LiveMonitor sample-feed/gateway, Pause/Cancel, Alerts/Export, agentic trajectory inspector (use the embedded Inspect viewer), tweaks/variations/design-canvas.

None of the deferred items are regressions — they're either fabricated-in-prototype-only or require a backend surface that doesn't exist yet.
