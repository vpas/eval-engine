# Eval Engine — Sandboxing for Agentic Evals (v1)

> Companion to `DESIGN.md`, `docs/ORCHESTRATION.md`, `docs/PLUGINS.md`. Agentic evals execute
> **untrusted, model-generated code** (shell, code, file ops). This is the highest-stakes security
> surface in the system, so "rely on Inspect's Docker sandbox" is **not sufficient on Kubernetes** —
> this doc specifies a tiered, K8s-native model. **v1 runs hardened, air-gapped per-sample pods** (the
> high-value, cheap part); the pooled microVM sandbox service that lifts the throughput ceiling is the
> trigger-gated next step — `docs/FUTURE.md` §4.

---

## 1. Threat model

What model-generated actions can do if unconstrained:
- **Destructive / runaway:** fork bombs, fill disk, peg CPU → DoS to co-tenants.
- **Network exfiltration / abuse:** phone home; scan the internal network; **attack the
  engine's own services** (Postgres, LiteLLM gateway); hit cloud metadata
  (`169.254.169.254`) to steal node IAM credentials.
- **Sandbox escape:** break container → node → other tenants / control plane.
- **Adversarial-by-design evals:** some evals *test* dangerous capabilities (cyber-offense,
  self-propagation). Adversarial on purpose → demand the strongest isolation available.

---

## 2. Key architectural clarity: the sandbox does not call the model

In Inspect's model the **agent loop runs in the worker**, and only **tool/command execution**
happens inside the sandbox. The sandbox therefore **almost never needs network to the model**
(model calls go worker → LiteLLM). Consequence: **most sandboxes can be fully air-gapped
(zero egress)** without breaking the eval. This removes the single largest exfiltration
vector for the majority of evals and is the foundation of the "air-gap by default" stance.

---

## 3. Why not "just use Docker" on K8s

Inspect's default Docker sandbox needs a Docker daemon. On K8s that means **Docker-in-Docker
(privileged)** or **mounting the host Docker socket** — privileged-container anti-patterns
that *undermine the isolation we're buying*. Instead we use **Inspect's Kubernetes sandbox
provider**, which provisions an **ephemeral pod per sample** as the sandbox; we then harden
those pods (§4–§5).

---

## 4. Tiered isolation by eval risk

One size is wrong (air-gapped microVMs for a calculator is wasteful; a shared container for a
cyber eval is reckless). The **harness config declares its tier** (PLUGINS §3 `sandbox`
block); the orchestrator provisions accordingly.

| Tier | Eval type | Runtime | Network | Placement |
|---|---|---|---|---|
| **T1 — benign tools** | web-search, calculator, RAG | hardened container | **deny-all + allowlist** to specific tool endpoints via egress proxy | shared sandbox node pool |
| **T2 — code execution** | general agentic, code tasks | **gVisor** (runsc), user-space kernel / syscall interception | **air-gapped** by default | isolated, tainted sandbox node pool |
| **T3 — dangerous capability** | cyber, self-propagation, adversarial | **Kata / microVM**, hardware-virtualized kernel boundary | **fully air-gapped**, no internal reachability | dedicated isolated node pool; ideally separate cluster/account |

**Default tier = T2** unless the harness opts down to T1 (needs a tool endpoint) or up to T3.

**The tier boundary criterion is sharp:** it's not "how dangerous does it feel" but **"is the
model adversarially trying to escape?"** gVisor (T2) reimplements the syscall surface in user
space → closes the *kernel-exploit* escape vector (most container escapes are kernel CVEs),
which is plenty when the model just wants to solve the task. Kata (T3) gives each pod a real
VM with its own kernel → the escape boundary becomes the **hypervisor**, which is what you need
when the eval *rewards* finding an exploit (cyber-offense capability tests). Pay for VM
isolation only when escape is the adversary's goal.

---

## 5. Baseline hardening (ALL tiers)

- **Resource caps** — CPU / memory / ephemeral-storage / **PID limit** per pod. The PID
  limit kills fork bombs; storage limit kills disk-fill.
- **No credentials** — no mounted secrets; **zero-RBAC service account**
  (`automountServiceAccountToken: false`); **block IMDS** (`169.254.169.254`) so a sandbox
  can't lift node cloud credentials.
- **Network default-deny** — `NetworkPolicy` denies all ingress/egress. Sandboxes **cannot
  reach Postgres, the gateway, or each other**. Legitimate needs (a web-search tool) route
  through a **controlled, logged egress proxy** with an allowlist (§6).
- **Pod security** — non-root user, **read-only root filesystem**, `drop ALL` capabilities,
  `seccomp=RuntimeDefault`, no privilege escalation, `hostNetwork/hostPID/hostIPC=false`.
- **Node isolation** — sandbox workloads on **dedicated tainted node pools**, away from
  control-plane / worker / gateway nodes, so an escape lands on a disposable node. T3 ideally
  a **separate cluster or cloud account**.
- **Ephemeral + TTL** — pods are single-use and time-bounded; the orchestrator guarantees
  teardown even on sample failure (ties to lease/heartbeat, §7).

---

## 6. Egress control (the biggest lever)

- Default **deny-all egress**; **no raw internet** from any sandbox.
- Where a tool needs network (T1), route through a **dedicated, credential-less egress proxy**
  — a **separate trust zone** from the LiteLLM model gateway (which carries *trusted* traffic
  and *holds provider API keys*). The sandbox proxy carries *untrusted, model-generated*
  traffic, so it **must never hold API keys** (else sandboxed code could spend your model
  budget or exfiltrate keys). Same egress *philosophy*, distinct trust zones — do not merge.
  - **Per-eval FQDN allowlist** (e.g. only `api.search-provider.com`).
  - **Full request logging** → an **audit trail of everything the agent accessed**
    (feeds the transcript). The proxy is also an **instrument**: "did the agent
    *attempt* disallowed network access?" is itself evaluable safety signal.
  - TLS-terminating or CONNECT-proxy.
- **CNI: Cilium** — enforces `NetworkPolicy`, supports **FQDN-based egress** policy and flow
  logging (portable across clouds). IMDS block + internal-service deny encoded as policy.

---

## 7. Lifecycle & orchestrator integration

When a worker claims an **agentic** sample-task (ORCHESTRATION §5):
1. **Provision** the tier's sandbox pod (image pinned by the eval's `code_ref`).
2. Run the Inspect solver; tool calls execute **inside** the pod; model calls go worker→LiteLLM.
3. **Commit result** + transcript (ORCHESTRATION §4), then **tear down** the pod.
4. **Guaranteed teardown** even on failure/timeout/crash: a **sweeper** reaps pods whose
   owning task's lease has expired (labels carry `run_id`/`sample_id`), so orphaned sandboxes
   can't accumulate.
- **Lease/heartbeat** must budget for **sandbox provisioning time** (pod schedule + start);
  the worker heartbeats during long agentic samples so the pod isn't reaped mid-run.

---

## 8. Scale, latency & cost

**v1 = per-sample pods.** Each agentic sample gets a fresh, hardened, ephemeral pod (§5) torn down on
exit. This is correct and within budget while agentic is a minority of the aggregate throughput.
Cold-start is mitigated:
- **Pre-pulled images** on sandbox nodes (DaemonSet warmer / node image cache).
- **`expanding`-triggered pre-warm:** when a run is admitted and its harness declares an agentic tier,
  pods are warmed *during* dataset expansion — so by the time workers claim, sandboxes are ready and
  cold-start hides behind work already underway.

**The ceiling.** Pod create/destroy **throughput** (not boot *latency*) tops out at low-hundreds/s —
K8s control-plane limits (scheduler/kubelet/IPAM/gVisor-boot). Warm pools of pre-*booted* single-use
pods hide latency but don't move this throughput ceiling. When agentic volume approaches it, the
mitigation is a **pooled sandbox service** (per-tier; T2 = microVM snapshot-restore) that keeps the
kube-scheduler out of the per-sample path — a **trigger-gated future build**, `docs/FUTURE.md` §4. This
is a **top-tier DESIGN §12 risk**; gate agentic-at-scale on a measured churn spike.

- **Startup overhead:** gVisor = modest; Kata/microVM = higher (real VM boot). Tier choice is also a
  latency/cost choice.
- **Images:** versioned, **vulnerability-scanned**, registry-stored, pinned by `code_ref` for
  reproducibility; minimal base to shrink attack surface + pull time.
- **Right-size** resource caps and **bin-pack** sandbox node pools; autoscale the pools with
  run demand (scale-to-zero when idle).

---

## 9. Portability

- **gVisor / Kata** are `RuntimeClass`es on every major managed K8s (GKE Sandbox ships
  gVisor; EKS/AKS install via node bootstrap). **NetworkPolicy** is standard (Cilium CNI).
- **T2 microVM snapshot-restore needs a KVM-capable node pool** (Firecracker/Kata are hardware-virt) —
  *not* the default managed nodes. To keep "portable by interface" honest: express T2 as a
  **`RuntimeClass`** (so the eval contract is runtime-agnostic), use **Kata as the portable fallback**
  where raw Firecracker isn't available, and provision the KVM-capable pool (GKE Sandbox / bare-metal /
  nested-virt) via the same Terraform node-pool module. The app/eval contract stays cloud-neutral; only
  the node-pool setup is cloud-specific.
- Cloud-specific bit = node-pool runtime setup, abstracted behind **Terraform** node-pool
  modules. The app/eval contract (tier + policy) is cloud-neutral.

---

## 10. Trust-boundary note (vs PLUGINS §7)

Two distinct boundaries — don't conflate:
- **Plugin code** (harness/scorer/tool *implementations*) runs **in the worker**; trusted via
  review (PLUGINS §7).
- **Model-generated actions** run **in the sandbox**; untrusted, isolated by this doc.

The sandbox protects against what the *model* does, not against a malicious plugin author.
Multi-tenant untrusted *plugins* would need their own isolation (deferred with tenancy).

---

## 11. Recommendation summary

v1 adopts **Inspect's K8s sandbox provider** with **hardened, air-gapped per-sample pods** and the
**three-tier** model (T1 hardened container / **T2 gVisor, the default** / T3 Kata-microVM): **air-gap by
default** (leveraging §2); **deny-all egress** with a **logged allowlist proxy** for the rare network
tool; **dedicated tainted node pools** (T3 on a separate account); full **baseline hardening** on every
tier; and a **sweeper** guaranteeing teardown. Proportionate: cheap isolation for benign evals,
hardware-VM isolation only where the eval is genuinely dangerous.

When agentic create/destroy throughput approaches the per-sample-pod ceiling (§8), the next step is a
**pooled sandbox service** (per-tier; T2 = microVM snapshot-restore) — a trigger-gated future build,
`docs/FUTURE.md` §4. The security tiering above is unchanged by that; only the *provisioning* mechanism
moves from per-sample pods to a pool.

---

## 12. Open questions
1. **Snapshot-restore pool sizing & golden-image management** — restore-pool depth
   by concurrency; idle-cost budget; golden-snapshot build/versioning (pinned by `code_ref`). T2 uses
   snapshot-restore (fresh per sample), so there is **no secure-reset guarantee to prove** — only T1
   reuses. **Top-tier risk — needs a churn + restore spike before agentic-at-scale.**
2. **T3 separation** — separate node pool within-cluster vs separate cluster vs separate cloud account? (Strength vs ops cost.)
3. ~~**Egress proxy reuse**~~ — **RESOLVED: dedicated, credential-less proxy**, separate trust zone from the LiteLLM gateway (§6).
4. **Per-eval network allowlists** — who authors/approves them; default-empty vs templated per tool.
5. **GPU-in-sandbox** — do any agentic evals need GPU *inside* the sandbox (e.g. ML-agent tasks)? Changes node pool + isolation story.
6. **Multi-container environments (DEFERRED — out of scope for now)** — evals needing a
   topology (attacker + victim hosts, internal micro-segmented network) via the k8s provider's
   multi-pod support. Noted for later: would add a `topology` concept to the harness config
   (declared pods + allowed internal edges), atomic multi-pod provision/teardown, sweep-by-
   topology, and is **T3-only** (the outer boundary contains deliberately-successful exploits).
   Single-pod sandboxes are the only supported model in v1.
