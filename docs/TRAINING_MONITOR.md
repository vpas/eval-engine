# Eval Engine — Training Monitor (continuous checkpoint evaluation)

> **vNext design doc** (a new track, not a v1 gap). DESIGN.md §1 lists *training/fine-tuning* as a v1
> **non-goal** — and this subsystem keeps us on the right side of that line: **the trainer is out of
> scope and mocked.** What we build here is a *continuous-eval / training-monitor* layer that **consumes
> a stream of checkpoints** produced by some external trainer and drives the **existing** eval machinery
> against each one — then surfaces the per-checkpoint score trajectory, with anomaly detection that
> distinguishes a *training fault* from *a model that is simply weak on an eval.*
>
> The **data model in §2/§3/§8 is derived from the interactive UX prototype** (the "Training" tab of the
> Eval Portal — `training-data.js`, `training.jsx`, `training-drill.jsx`, `training-chart.jsx`). The full
> UI is implemented later; for now the prototype is the **source of truth for the data shape** the
> backend must support.
>
> Companion to `DESIGN.md` (domain model §7, reproducibility §14), `SCHEMA.md` (the projection),
> `SCHEDULER.md` (lanes), and `FUTURE.md` (the defer-until-proven discipline this doc follows).
> Status: **design draft** — converging. Not yet on the v1 backlog (`docs/PROJECT_PROGRESS.md`).

---

## 1. Problem & scope

We have training runs (mocked) that emit **consecutive checkpoints** as training progresses. We want to,
*while training is still running*:

1. **Monitor** a running training and **get notified of new checkpoints**.
2. **Evaluate** each new checkpoint on a configured **eval suite** (reusing the whole engine).
3. **Show** training progress and per-checkpoint eval scores in a dedicated **Training** tab.
4. **Detect anomalies** — eval scores that deviate from their expected trajectory and may point to a
   training problem.
5. **Diagnose** — tell a *training error* (bad checkpoint, divergence, serving fault, stuck pipeline,
   alignment drift) apart from *the model just performing badly* on a hard eval.

### Scope boundary (what is and isn't ours)

| Concern | In scope (this doc) | Out of scope (trainer-side, mocked) |
|---|---|---|
| Producing checkpoints | — | training loop, optimizer, data pipeline |
| **Serving** a checkpoint for inference | — (we receive a `model_ref`) | standing up vLLM per checkpoint |
| Announcing a checkpoint | the **storage manifest contract** we read | the trainer writing it |
| Evaluating a checkpoint | **yes** — reuse Runs/ledger/workers/analytics | — |
| Trajectory UI + anomaly/diagnosis | **yes** | — |

The guiding reuse insight (confirmed by the prototype): **a checkpoint-eval is just a `Run`** (DESIGN §7),
tagged with `(training_run_id, checkpoint_id, step)` and a **`sweep`** group. So the ledger, KEDA workers,
ClickHouse projection, retries, budget caps, transcripts, the embedded Inspect viewer, **and the existing
Compare view** all work **unchanged** — the prototype literally diffs two checkpoint-runs in Compare. Net
new surface is thin: a few entities, a few **nullable** columns, one poller/monitor role, an anomaly
module, and a UI tab.

---

## 2. Domain model additions

Additive, in the orthogonal-blocks style of DESIGN §7. Field names mirror the prototype's `training-data.js`.

### `TrainingRun` (registered entity, versioned/immutable like datasets/evals/models)

```
{ id            "tr-atlas-7b-0521"
  model         "atlas-7b"            # the model being trained (display + ckpt model-id prefix)
  base          "atlas-base-7b"       # base/seed model
  status         training | completed | failed | stopped
  current_step  64000
  planned_steps 120000                # → progress bar = current/planned
  started        ISO-8601
  owner          email                # created_by
  hardware      "256× H100"           # provenance (display)
  precision     "bf16"                # provenance (display)
  glyph         "A7"                  # 2-char avatar (display)
  suite         [eval_id…]            # evals run on EVERY checkpoint
  track         [ EvalTrack… ]        # per-eval display + expected-curve params (below)
}
```

- **`EvalTrack`** `{ id (eval_id), color, ceil, tau }` — per-eval metadata: a stable chart `color`, and
  the **expected-trajectory** params `ceil`/`tau` (the saturating curve `expected(step)=ceil·(1−e^(−step/τ))`;
  see §8). In production `ceil/tau` are *fitted* from observed checkpoints, not authored.
- **`suite`** is the set of registered evals (each `eval@version`) — every checkpoint runs all of them so
  every step is comparable.

### `Checkpoint` (one per discovered checkpoint)

```
{ id, training_run_id, step, idx,
  model_ref      "checkpoint:tr-atlas-7b:64000"   # opaque handle the engine evaluates (§4)
  tokens         1.34e11      # cumulative tokens seen (the alternate x-axis)
  wall_time      ISO-8601
  status          discovered | evaluating | evaluated | error | skipped
  train_metrics { loss, grad (grad-norm), lr, throughput }   # optional trainer telemetry (§8 cross-check)
}
```

The **per-eval scores** for a checkpoint are *not* stored on the Checkpoint row — they live in the
analytics projection (§7), keyed `(training_run_id, eval_id, step)`, because they're produced by the
checkpoint-eval Runs. The Checkpoint row carries only what the trainer announces.

### Threaded onto existing tables (all **nullable** — no hot-path reshape)

- `runs.training_run_id`, `runs.checkpoint_id`, `runs.step`, **`runs.sweep`** — a checkpoint-eval Run is
  tagged; an ordinary ad-hoc Run leaves them NULL and behaves exactly as today. `sweep` (e.g.
  `"ckpt-atlas-7b"`) groups a checkpoint sweep so the runs list / Compare can collect them. The Run's
  `model` is the **step-stamped** id (`"atlas-7b/step-64k"`), so it reads naturally in existing views.
- ClickHouse `sample_results` gains `training_run_id`, `step` (NULL/empty for ad-hoc runs) so the
  time-series query (§7) is a `GROUP BY step` with no new store.

A checkpoint-eval Run's `RunSpec` is otherwise normal — `eval@version` from the suite, `model =` the
checkpoint's `model_ref`, default harness/scorers from the eval, epochs/budget per the training run's
config. Reproducibility (DESIGN §14) is inherited for free, **plus** the `(training_run_id, step)` provenance.

---

## 3. The checkpoint interface — storage poller

**Decision: a storage poller is the sole interface** (no trainer→engine HTTP calls). The trainer writes a
**manifest** per checkpoint to a watched object-storage prefix; the monitor lists the prefix and reacts to
new manifests. Rationale: zero trainer-side coupling, and it goes through the existing
`eval_engine/storage.py` fsspec abstraction (#14) so the watched prefix is `gs://…` in-cluster and a local
dir in dev with no code change.

### Layout & manifests

```
gs://<bucket>/training/<training_run_id>/
    run.json                      # TrainingRun manifest (written at start; status updated on finish)
    step_0064000/checkpoint.json  # presence of checkpoint.json = "ready to eval" (commit signal)
    …
```

`run.json` — everything in the `TrainingRun` entity that the trainer owns (`model, base, planned_steps,
started, owner, hardware, precision, suite[]`). `track` (colors/ceil/tau) is derived/fitted by the engine,
not the trainer.

`checkpoint.json` — the per-checkpoint contract (nothing about *how* it was trained):

```json
{
  "training_run_id": "tr-atlas-7b-0521",
  "step": 64000,
  "model_ref": "checkpoint:tr-atlas-7b-0521:64000",
  "tokens": 134400000000,
  "wall_time": "2026-06-05T12:34:56Z",
  "train_metrics": { "loss": 1.72, "grad": 0.40, "lr": 1.6e-4, "throughput": 2950 }
}
```

- **Write-then-commit.** The trainer writes the checkpoint payload first, then the manifest last, so the
  monitor never evaluates a half-written checkpoint; `checkpoint.json` existence is "ready."
- **`train_metrics` optional** but, when present, drive the §8 cross-check (overfitting vs divergence vs
  stuck vs serving-stall). The mock supplies them (and perturbs them at an injected fault).

### `CheckpointSource` interface

```python
class CheckpointSource(Protocol):
    def list_training_runs(self) -> list[TrainingRunManifest]: ...
    def list_checkpoints(self, training_run_id: str) -> list[CheckpointManifest]: ...
```

- `StoragePollingSource` — the default; lists the prefix via `storage.ls` (a small additive helper on the
  fsspec wrapper), diffs against already-discovered steps in Postgres → new checkpoints.
- (A push receiver could be added behind the same interface later — deferred, §12.)

---

## 4. Mock serving — checkpoint `model_ref` → real model

There is **no real trainer and no real per-checkpoint serving** (the non-goal). A checkpoint's `model_ref`
is an **opaque handle** the engine evaluates *as if* trainer infra served it — and a thin **mock resolver**
maps that handle to a real, callable model behind the scenes.

- **The rest of the engine never knows.** The runner launches a Run with `model =
  "checkpoint:tr-atlas-7b-0521:64000"`; the gateway/runner resolves it to a real model id at call time. To
  the ledger, analytics, UI, and reproducibility plumbing it's just a model served elsewhere.
- **The mock owns the mapping** `checkpoint_ref → real OpenRouter model (+ knobs)` — the single place
  reality is faked, and **how we script the experiment**:
  - **A trajectory** = a sequence of underlying models of increasing capability across steps (weak early →
    strong late ⇒ a rising, saturating accuracy curve — matching the prototype's `ceil·(1−e^(−step/τ))`).
  - **A fault is an injected mapping** at a chosen step: a garbage/repetition-inducing system prompt, a
    temperature cranked to incoherence, an intentionally-wrong tiny model, or a non-existent id (a serving
    outage → sample **errors**, not low scores). Each fault trips a specific §8 detector, so the mock
    doubles as the subsystem's integration-test fixture (it reproduces the prototype's injected anomalies:
    a gsm8k regression at 34k with a loss/grad spike, a slow toxicity drift, a humaneval plateau).

**Resolution site (open):** a LiteLLM **model alias** per checkpoint (`checkpoint:… → openrouter/<model>`,
most faithful — the gateway does the indirection it already does for every model) vs. a **runner-side
rewrite** (simpler to script dynamically from the mock). Leaning alias-for-fidelity; rewrite acceptable for
the prototype. The `MockTrainer` writes manifests **and** registers/updates the mapping per step.

---

## 5. The Training Monitor role

One new **thin, leader-elected** loop (the orchestrator pattern: derive-state-each-tick; PG advisory lock;
crash just lets discovery lag a few seconds — governs *fan-out*, not safety). Per tick, for each
`watching`/`training` TrainingRun:

1. **Discover** — `CheckpointSource.list_checkpoints` diffed against Postgres → insert new `Checkpoint`
   rows (`discovered`).
2. **Fan out** — for each new checkpoint, launch the **eval suite** = N normal Runs (`runner.launch`) with
   `model = checkpoint.model_ref`, tagged `(training_run_id, checkpoint_id, step, sweep)`. Mark `evaluating`.
3. **Reconcile** — when a checkpoint's suite Runs all reach a terminal state, mark `evaluated`, refit the
   per-eval expected curve, then run §8 anomaly checks for that step and persist any anomalies.
4. **Finalize** — when the run manifest flips terminal (or discovery times out): mark terminal, compute the
   run summary (best checkpoint per eval, §10).

**Lane.** Checkpoint-evals run in the **interactive lane** (SCHEDULER §2) — small, frequent, you want
*timely* feedback while training is live; they must not queue behind a 100k-sample batch run. (The
prototype tags the synthesized runs `lane: batch`; we default interactive and let the training-run config
override.)

**Backpressure.** Training saves faster than evals complete. Per-run policy: **eval-every-checkpoint with a
skip-stale safety valve** — if the un-evaluated backlog exceeds `max_pending_checkpoints`, drop intermediate
checkpoints and keep the **latest** (the curve matters, not every point); skipped checkpoints get
`status=skipped` and gap the chart. `every Kth` / `eval all` are config knobs over the same loop.

---

## 6. What we reuse unchanged (the cheap part)

| Existing mechanism | Used as-is for checkpoint-evals |
|---|---|
| `Run` + `RunSpec` (§7) | a checkpoint-eval *is* a Run, tagged + `sweep`-grouped |
| Ephemeral ledger + `FOR UPDATE SKIP LOCKED` claim | shards each checkpoint-eval like any run |
| KEDA worker Deployment | autoscales on the same queue depth |
| ClickHouse projection + `by_category` | the score-vs-step series **and** per-category regression (§8) |
| Wilson CI (#4) | significance gate — no "drop" inside the noise band |
| Retries / `failed` vs low-score split (#2) | the error-rate diagnostic (§8) |
| Budget caps / `budget_skipped` (#3) | a training run gets an aggregate budget too |
| Transcripts + Inspect viewer (#15) | drill from a checkpoint/anomaly into per-sample transcripts |
| **Compare view** | **diff two checkpoint-runs** (step-A vs step-B) per the prototype's "Diff in Compare" |
| Audit log (#13) | checkpoint discovery + fan-out are audited |

Net new code: entities + columns, the `CheckpointSource`/poller, the monitor loop, the mock resolver +
`MockTrainer`, the expected-curve fit + anomaly module, the score projection, and the UI tab.

---

## 7. Analytics — the score-vs-step series

The hero query is "**eval score (with CI) vs. step**, per eval in the suite." With the provenance columns
on `sample_results`, that's a rollup over the *existing* projection — no new store:

```sql
SELECT eval_id, step,
       count()              AS n,
       avg(passed)          AS accuracy,        -- Wilson CI computed app-side from (passed, n)
       countIf(error <> '') AS sample_errors    -- the error-rate diagnostic (§8)
FROM sample_results
WHERE training_run_id = {tr}
GROUP BY eval_id, step ORDER BY eval_id, step
```

A small **`checkpoint_scores`** materialization (or the live query — a training run is ~tens–hundreds of
checkpoints × a few evals, tiny vs. the 12B-row main store) gives `(training_run_id, eval_id, step) →
{accuracy, ci_lo, ci_hi, n, sample_errors, expected, output_health…}`. `expected` is the fitted curve value
(§8); `tokens`/`loss`/`grad`/`lr`/`throughput` per step come from the `Checkpoint` row, joined for the
x-axis toggle and loss overlay.

**Two derived views the prototype needs** (both off existing data, no new storage):
- **Eval × checkpoint matrix** (heatmap) — `accuracy` per `(eval, step)`; a cell is *flagged* when
  `expected − accuracy > threshold`.
- **Regressed-sample diff** — per-sample pass/fail at step-B vs step-A on one eval → "passed before, fail
  now." This is exactly a **Compare** of the two checkpoint-runs; reuse it.

---

## 8. Anomaly detection & diagnosis

Two jobs: **(A) detect** a score that deviates from where it *should* be, **(B) diagnose** whether it's a
*training fault* or *a model genuinely weak on a hard eval*. The prototype's anomaly object is the target
shape:

```
{ id, eval, step, kind: regression|drift|plateau, severity: high|medium|low,
  delta,        # Δ vs EXPECTED (pp) — the meaningful regression metric, not raw step-to-step drop
  from,         # baseline step the regression is measured against
  cause,        # root-cause hypothesis (narrative)
  signals:    [ { k, v, note, bad } … ],     # correlated signals (the corroboration evidence)
  categories: [ { cat, acc, prev } … ],      # per-category regression (acc now vs prev)
  samples:    [ sample_id … ] }              # regressed samples: passed before, fail now
```

### Expected trajectory (the spine of detection) — reconciling with the earlier "defer" call

The prototype's **core regression metric is deviation from an expected curve** (`delta = actual −
expected`), and the chart's **expected band** is `expected ± threshold`. Last round we chose CI-aware deltas
+ canary + corroboration and **deferred trajectory modeling**. Reconciliation — a principled middle path:

- **Ship a *simple* expected curve in v1** (the saturating fit `ceil·(1−e^(−step/τ))`, fit per eval from
  observed checkpoints, or an EMA fallback early on). It powers the band + the `delta-vs-expected` metric
  the whole UX is built on. This is cheap and not the "novel IP" we were avoiding.
- **Keep CI-awareness** as the significance gate: a deviation only becomes an anomaly when it clears the
  Wilson noise band *and* the threshold — so we don't flag sampling noise.
- **Keep canary evals** as an orthogonal discriminator (§B).
- **Still defer** the *heavy* version — a Gaussian-process / isotonic fit with calibrated uncertainty —
  behind a trigger (the simple curve proves too coarse on real, non-monotone trajectories).

### (A) Detectors → `kind`

1. **`regression`** — a CI-clearing drop where `expected − actual > threshold` at a step (often a cliff).
   Catches a bad checkpoint, an LR-scheduler event, a corrupted data shard.
2. **`drift`** — a slow monotonic decline over several checkpoints (the prototype's toxicity case:
   "alignment tax" as raw capability rises). Catches safety/alignment erosion, slow over-fitting.
3. **`plateau`** — flat across several checkpoints when the curve should still climb, often localized to one
   category (the prototype's humaneval/recursion case). Catches a stuck capability / data-mix gap.

`severity ∈ {high, medium, low}` from `|delta|` × persistence × breadth. The **alert threshold is a live
knob** (pp), not a fixed constant: `activeAnomalies(thr)` filters by `|delta|·100 ≥ thr`, and the band is
sized by the same `thr`. So the store keeps each anomaly's `delta`/magnitude and the UI/query thresholds.

### (B) The discriminator — training fault vs. weak model

Core idea: **corroborate the eval score with orthogonal signals to localize the fault** — this is exactly
the prototype's `signals[]` array. A model *genuinely bad at math* still emits coherent wrong answers,
*stably*, on that *one* eval; a *broken checkpoint / serving fault* emits garbage, *suddenly*, across
*everything*. Signals (each already produced or cheap to add), shown as `{k, v, note, bad}`:

- **Training-metric cross-check** (the prototype's headline signals: `train loss +0.13`, `grad-norm 1.81 =
  4.5× baseline`, `throughput −13% shard stall`, `lr stable`):

  | train loss | eval score | diagnosis |
  |---|---|---|
  | ↓ | ↓ | **overfitting / reward-hacking / train-eval mismatch** (genuine) |
  | ↑ or NaN | ↓ | **training divergence** (optimization/infra failure) |
  | flat | flat | **stuck pipeline** (dead training) |
  | spike + grad-norm spike + throughput dip | ↓ | **corrupted data shard / LR event** (the gsm8k case) |
  | ↓ | ↑ | healthy |

- **Broad-vs-isolated collapse.** Simultaneous catastrophic drop across **all** evals ⇒ training/infra. An
  isolated, stable low score ⇒ the model is simply weak there.
- **Canary / anchor evals.** Every suite includes 1–2 **trivial** sanity evals (`eval.role = "canary"`) any
  *functional* model scores ~100% on. **Canary collapses ⇒ model/serving broken (training fault). Canary
  fine but a hard eval low ⇒ the model is just not good at the hard task.** The cheapest decisive
  discriminator.
- **Error-rate vs. low-score.** The engine already separates execution **errors** (retries, `failed`, 5xx,
  sandbox timeouts) from low **scores**. A spike in *sample errors* ⇒ **serving/harness** broke, not the
  model.
- **Output-health / degeneracy.** Cheap per-checkpoint signatures from transcripts (empty-output rate,
  repetition/looping, single-token collapse, parse-failure rate, refusal rate) — they move on a *fault*,
  stay flat when a model is merely weak. (The prototype's "refusal rate −7pp" is one of these.)
- **Category-level regression.** `categories[]` (acc vs prev per category) — reuse the existing
  `by_category` analytics; localizes the regression (algebra/geometry collapsed, arithmetic held).

### Output: diagnosis + narrative cause

Each anomaly carries `cause` — a **root-cause hypothesis** (the prototype shows a sentence: *"Sharp
regression coincident with a training-loss spike. Most consistent with a corrupted data shard near step 33k,
or an LR-scheduler restart."*). v1 generates this by **templating from the dominant signals** (which quadrant
of the cross-check + which signals are `bad`); an LLM-judge-style narrative is a later refinement. The
combiner also yields the underlying label (`bad-checkpoint · serving/infra-error · training-divergence ·
overfitting/reward-hacking · alignment-drift · stuck/plateau · model-weak-on-eval · healthy`). Persisted to
a Postgres `anomalies` table; rendered as chart markers + the drill drawer.

---

## 9. Web UI — the Training tab (from the prototype)

A top-level **Training** tab. The full UI is implemented later; this captures what the **data model must
feed**:

- **Run header** — model picker (switch between training runs), status pill (`training`→running /
  `completed`), `in-house` badge, `tr-…` id + `base`. A **training-progress bar** (`current_step /
  planned_steps · %`). Header stats: `tokens`, `train loss`, `evals tracked`, `hardware`, `started (ago)`.
  An **anomaly tag** (`N anomalies · ≥{thr}pp` in red, or "no anomalies" in green). Actions: **overlay**
  picker (cross-run ghost), **Latest checkpoint** (opens the inspector), **Alerts**, **Export**.
- **Controls** — per-eval **chips** (toggle visibility, eval color), **expected band** toggle, **loss
  overlay** toggle, **steps ↔ tokens** x-axis segment, and the **alert-threshold slider** (0.5–10 pp →
  `band = expected ± thr`, and filters active anomalies).
- **Hero chart** ("Eval accuracy across checkpoints") — multi-line, one line per visible eval, x = step (or
  tokens), y = accuracy; **expected-band corridor** (`expected ± thr`) + dashed expected line per eval; a
  **loss overlay** on a secondary axis; a shaded **future region** past the current step with a "now ·
  {step}" marker; **anomaly markers** on the curve; a **cross-run ghost overlay** (dashed, e.g. atlas-6b);
  hover-to-scrub, click a checkpoint → drill.
- **Eval × checkpoint matrix** (heatmap) — evals × checkpoint steps, cell = accuracy %, color-scaled
  low→high, **flagged cells outlined** when `expected − acc > thr`; click an anomaly cell → drill.
- **Anomalies panel** — list sorted by severity: severity dot, `eval`, `kind` tag, `delta` (pp), the `cause`
  sentence, `@ step` / `vs {from}`, "inspect ›".
- **Drill drawer** (anomaly) — root-cause hypothesis box; `Δ vs expected` / `at step` / `baseline` delta
  boxes; **correlated signals** grid (`{k, v, note}`, green/red by `bad`); **category regression** bars (acc
  vs prev); **regressed samples** list ("was pass → fail"); buttons: **Diff checkpoints in Compare** (opens
  the real step-A vs step-B checkpoint runs in the per-sample diff) and **Transcripts**.
- **Drill drawer** (checkpoint inspector) — step / tokens / loss / grad-norm boxes; **movers vs previous
  checkpoint** (per-eval Δ bars, sorted by biggest drop); "Diff vs step-{prev}" button.

Dependency-light (hand-rolled SVG chart, consistent with DESIGN §8).

---

## 10. Lifecycle & outputs

- **Start** — a `run.json` appears → monitor begins `watching`.
- **Run** — checkpoints discovered → evaluated → scored → curve refit → diagnosed, live.
- **Finish** — manifest flips terminal (or discovery times out) → finalize: **best checkpoint per eval**
  (argmax with CI tie-break, optionally constrained to canary-passing checkpoints), a trajectory summary,
  the anomaly list. Best-checkpoint = a natural "ship candidate" output.
- **Stop/cancel** — a user can stop monitoring; in-flight checkpoint-evals finish or cancel like any run.

---

## 11. Decisions (this track)

| Concern | Decision |
|---|---|
| Subsystem placement | **vNext track** (training is a v1 non-goal); trainer mocked, eval layer real |
| Checkpoint interface | **Storage poller** over a watched prefix (manifest = commit signal); fsspec (#14) |
| Checkpoint serving | **None built** — opaque `model_ref` + a **mock resolver → OpenRouter** model |
| Reuse | a checkpoint-eval **is a tagged + `sweep`-grouped `Run`**; ledger/workers/analytics/**Compare**/viewer reused |
| Schema change | **additive** — 2 entities + 4 nullable `runs` cols (+2 on `sample_results`) + score/anomaly tables |
| New role | one **leader-elected Training Monitor** loop (discover → fan-out → reconcile+refit → finalize) |
| Lane | checkpoint-evals run **interactive** (timely feedback), run-config overridable |
| Backpressure | **eval-every + skip-stale** safety valve (keep latest), per-run configurable |
| Anomaly metric | **deviation from a *simple* expected curve** (`ceil·(1−e^(−step/τ))` / EMA) — powers the band |
| Anomaly v1 | kinds `regression/drift/plateau` + severity + **signals/categories/regressed-samples** + a templated `cause`; **CI-gated**; canary as discriminator |
| Alert threshold | a **live pp knob**, not a fixed constant (sizes the band, filters active anomalies) |
| Heavy trajectory model | **deferred** (GP/isotonic with calibrated uncertainty) — trigger: simple curve too coarse |

---

## 12. Deferred behind a trigger (FUTURE.md discipline)

| Deferred | Re-introduce when… |
|---|---|
| **Push / webhook checkpoint source** | a real trainer wants low-latency push rather than be polled |
| **Real per-checkpoint serving** (vLLM spin-up from a weights URI) | a real trainer + the deferred vLLM path (FUTURE §3) land together |
| **Heavy expected-trajectory modeling** (GP/isotonic, calibrated bands) | the simple curve proves too coarse on real, non-monotone curves |
| **LLM-generated root-cause narrative** | the templated `cause` proves too shallow and there's appetite for it |
| **Auto-actions** (alert/page, auto-pause training, auto-rollback to best ckpt) | the diagnosis layer earns trust on real runs |
| **N-way cross-training comparison** (the prototype starts with a single ghost overlay) | users compare many candidate training configs head-to-head |

---

## 13. Open questions

- **Suite cost vs. cadence** — full suite every checkpoint can dominate spend; sub-sample the *dataset* per
  checkpoint (cheaper, noisier — wider CIs) and run the full set only on milestone steps?
- **`model_ref` resolution site** — gateway alias (fidelity) vs. runner-side rewrite (scriptability); §4.
- **Curve fit** — global saturating fit vs. EMA vs. piecewise; how to handle resumed/non-monotone runs and
  the cold-start (few checkpoints) where the expected band is undefined.
- **Canary authorship** — a shared canary pack vs. per-domain canaries; who owns it.
- **`cause` generation** — pure template from signals (v1) vs. an LLM judge over the signal bundle (later).
- **Anomaly de-dup** — a `drift` spans many steps; one anomaly with a range vs. one per step (the prototype
  collapses to one per eval).

---

## 14. Build phases

- **P1 — Spine.** Entities + provenance/`sweep` columns; `StoragePollingSource`; the Training Monitor loop
  (discover → fan-out → reconcile); `MockTrainer` writing `run.json`/`checkpoint.json` + the resolver
  mapping to OpenRouter; prove a scripted **rising curve** end-to-end (checkpoints → tagged Runs →
  score-vs-step query).
- **P2 — UI.** The Training tab: run header + progress, hero chart (lines, loss overlay, steps/tokens,
  expected band), heatmap, checkpoint-inspector drill → Compare integration; live polling.
- **P3 — Anomaly & diagnosis.** Expected-curve fit; canary role; `regression/drift/plateau` detectors
  (CI-gated); the `signals`/`categories`/regressed-samples corroboration; templated `cause`; `anomalies`
  table + chart markers + drill drawer + alert-threshold knob. Validate each detector with a
  **fault-injecting** `MockTrainer` scenario reproducing the prototype's three anomalies.
- **P4 — Finalize outputs.** Best-checkpoint selection; run summary; skip-stale backpressure; per-training-run
  budget; cross-run overlay/Export.
