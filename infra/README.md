# `infra/` — lifecycle scripts (local dev + cloud cost control)

Helper scripts for the two environments eval-engine runs in:

- **Local backends** (`up.sh` / `down.sh`) — Postgres + ClickHouse in Docker, for running the app
  locally. (The test suite self-provisions its own backends, so these are only for app dev.)
- **The GKE cluster** (`cloud-*.sh`) — a cost-control ladder from "pause overnight" to "delete
  everything" to "rebuild from scratch".

> The cluster costs money. Pick the lowest rung that fits how soon you'll be back.

## Quick reference

| Script | Purpose | Reversible? | Time | Bill after |
|---|---|---|---|---|
| `up.sh` / `down.sh` | local Postgres + ClickHouse (Docker) for app dev | yes | seconds | $0 (local) |
| `cloud-down.sh` | **pause** — system pool → 1 node, workers/sandbox → 0 | `cloud-up.sh` | ~1 min | ~$20–25/mo |
| `cloud-down.sh --full` | **deep pause** — ALL pools → 0 nodes | `cloud-up.sh` | ~3 min | LB + disks + control-plane |
| `cloud-up.sh` | **resume** a paused cluster (no redeploy) | — | ~3–5 min | full running |
| `cloud-destroy.sh` | **delete everything billable** (cluster + data + images + bucket) | `cloud-init.sh` (from scratch) | ~5–10 min | ~$0 |
| `cloud-init.sh` | **rebuild the whole system from scratch** | `cloud-destroy.sh` | ~30–40 min | full running |

---

## Local backends (app dev)

```bash
bash infra/up.sh      # Postgres on :5433, ClickHouse on :8123 (Docker containers ee-postgres / ee-clickhouse)
bash infra/down.sh    # remove them
```

The **test suite** (`pytest`) does **not** need these — `tests/conftest.py` reuses a running stack or
starts its own. `up.sh` is only for running the app against real backends locally
(`eval-engine run examples/capitals_qa.yaml`).

---

## Cloud: pause / resume (the common case)

The fastest way to stop most billing and come back quickly. Cluster, PVCs (data), config, the IP, and
images all persist, so resume needs **no redeploy**.

```bash
bash infra/cloud-down.sh          # pause:  system 3→1, workers/sandbox →0   (keeps 1 node for addons)
bash infra/cloud-down.sh --full   # deep pause: EVERYTHING →0 nodes (disables pool autoscaling first)
bash infra/cloud-up.sh            # resume: system →3 (HA), autoscaling restored, waits for rollout
SYSTEM_NODES=1 bash infra/cloud-up.sh   # cheaper non-HA resume (1 system node)
```

**Why `--full` is more than `system→0`:** a GKE Standard cluster won't sit at 0 nodes on its own —
control-plane addons (`konnectivity-agent`, …) become homeless and the autoscaler resurrects an
*expensive gVisor sandbox node* to host them. `--full` first **disables autoscaling** on the
workers/sandbox pools so nothing can scale back up; the addons sit `Pending` until you resume.
`cloud-up.sh` re-enables autoscaling + restores the system pool, so it self-heals.

While paused the app is **down** (HA ClickHouse/Redis/api won't fit on the reduced nodes → `Pending`).
That's expected; your data is safe on the PVCs.

---

## Cloud: destroy (going to ~$0) and rebuild

When you're done for a while and want the bill at zero.

```bash
# tear it ALL down (irreversible — deletes data, images, bucket):
bash infra/cloud-destroy.sh                 # prompts you to type the project id
CONFIRM=eval-engine bash infra/cloud-destroy.sh   # non-interactive

# ...later, rebuild the whole system from scratch:
set -a; source .env; set +a                 # provide the secret env (see below)
bash infra/cloud-init.sh
```

`cloud-destroy.sh` deletes the GKE cluster, the StatefulSet PVC disks (your data), the ingress load
balancer + forwarding rules + IP, k8s/gke firewall rules, the Artifact Registry repo (all images), and
the GCS artifacts bucket. It uses `gcloud` (not `terraform destroy`) because the PVCs + the
LoadBalancer's GCP resources are created dynamically by Kubernetes and would orphan after a bare
cluster delete — the script sweeps them. It's guarded (won't run without confirmation) and idempotent.

`cloud-init.sh` is the inverse — one command that rebuilds everything:

0. **preflight** — fails fast if a tool / secret env var / `terraform.tfvars` is missing.
1. **terraform apply** — cluster, node pools, reserved IP, IAM, GCS bucket, Artifact Registry,
   KEDA / ingress-nginx / cert-manager.
2. **auth** — `get-credentials` + `configure-docker`.
3. **build + push images** — app + frontend (`:<sha>` + `:latest`); the new AR repo starts empty.
4. **secrets.sh** — Neon DSN, OpenRouter, OAuth, litellm, cookie (from env).
5. **install.sh** — render + apply manifests, dashboards, wait for rollouts.
6. **seed evals** — the benchmark suite + `petri_audit` (snapshots the in-image JSONLs).

### `cloud-init.sh` prerequisites

- **Tools:** `gcloud`, `terraform`, `docker` (a usable daemon), `kubectl`, `python3`.
- **`deploy/terraform/terraform.tfvars`** — copy `terraform.tfvars.example`, set `project_id`/`region`/`zone`.
- **Secret env** (same vars `deploy/secrets.sh` needs — e.g. `set -a; source .env; set +a`):
  `EVAL_ENGINE_PG_DSN`, `OPENROUTER_API_KEY`, `OAUTH2_CLIENT_ID`, `OAUTH2_CLIENT_SECRET`.
- A reachable **Neon Postgres** at `EVAL_ENGINE_PG_DSN`.

### Two things about the destroy → init round-trip

- **The ingress IP changes.** `cloud-destroy` releases the IP, so `cloud-init`'s `terraform apply`
  reserves a *new* one → a new `<ip>.nip.io` host. **You must add `https://<host>/oauth2/callback` to
  the Google OAuth client's Authorized redirect URIs**, or sign-in 403s. This is the one step that
  can't be automated (it lives in the Google Cloud console); `cloud-init.sh` prints the exact URL.
- **Neon Postgres is external.** Neither `destroy` nor `init` touches it; the control schema
  auto-migrates on API startup. So your Neon DB (and old run rows) persist across a destroy/init cycle
  — reset it in the Neon console if you want a truly clean DB.

---

## What actually bills (GKE), by state

| Resource | Running | `cloud-down` | `--full` | `cloud-destroy` |
|---|---|---|---|---|
| Node VMs (system + workers + sandbox) | full | 1 system node | 0 | 0 |
| Load balancer + forwarding rule (ingress) | yes | yes | yes | **deleted** |
| Persistent disks (PVCs — your data) | yes | yes | yes | **deleted** |
| Artifact Registry (images) | yes | yes | yes | **deleted** |
| GCS bucket (logs/transcripts) | ~$0 (tiny) | ~$0 | ~$0 | **deleted** |
| GKE control-plane fee | ~$0.10/hr* | * | * | **gone** |

\* Likely **waived** — this is a single **zonal** cluster, and GKE gives one free zonal cluster per
billing account.

**Not present (verified):** no Cloud NAT, no extra reserved static IPs, no disk snapshots.

**External — not on the GCP bill:** **Neon Postgres** (control plane). Free tier autosuspends to ~$0;
manage it in the Neon console.

So a `cloud-down --full` leaves roughly **~$20–25/mo**, almost entirely the **load balancer** — the
price of keeping the IP + data ready for a quick resume. Only `cloud-destroy` removes that.

---

## See also

- `docs/DEPLOYMENT.md` — the GKE bring-up tracker (milestones M0–M11) and the canonical 3-step
  fresh-cluster bring-up that `cloud-init.sh` automates.
- `deploy/secrets.sh` / `deploy/install.sh` — the secret-creation + manifest-apply steps invoked by
  `cloud-init.sh`.
- `deploy/terraform/` — the infra definitions (cluster, pools, IP, IAM, GCS, Artifact Registry, Helm).
