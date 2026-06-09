#!/usr/bin/env bash
# From-scratch GCP bring-up — the inverse of cloud-destroy.sh. Rebuilds the WHOLE system so the
# dashboard works end-to-end: infra (Terraform) → secrets → container images → manifests → seed evals.
# Mirrors the documented 3-step bring-up (docs/DEPLOYMENT.md §7), plus the image build + eval seeding
# that a fresh (image-less, eval-less) cluster needs. Safe to re-run (Terraform/secrets/install are
# idempotent; images rebuild each time).
#
# YOU MUST PROVIDE:
#   • tools on PATH:  gcloud, terraform, docker (a usable daemon), kubectl, python3
#   • deploy/terraform/terraform.tfvars  (copy terraform.tfvars.example; set project_id/region/zone)
#   • secret env (same vars deploy/secrets.sh needs) — e.g. `set -a; source .env; set +a` first:
#       EVAL_ENGINE_PG_DSN   OPENROUTER_API_KEY   OAUTH2_CLIENT_ID   OAUTH2_CLIENT_SECRET
#   • a reachable Neon Postgres at EVAL_ENGINE_PG_DSN (external; cloud-destroy never touches it — the
#     control schema just auto-migrates on API startup).
#
# ONE MANUAL STEP afterwards (can't be automated): the ingress gets a NEW IP → a new <ip>.nip.io host,
# so add  https://<host>/oauth2/callback  to the Google OAuth client's Authorized redirect URIs, or
# sign-in 403s. The script prints the exact URL at the end.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TFDIR="$ROOT/deploy/terraform"
GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)"
EE_ZONE="${EE_ZONE:-us-central1-a}"; EE_REGION="${EE_REGION:-us-central1}"; EE_CLUSTER="${EE_CLUSTER:-eval-engine}"
EE_NAMESPACE="${EE_NAMESPACE:-eval-engine}"

echo "This CREATES billable GCP resources (a GKE cluster + LB + disks). Building from ${GIT_SHA}."

# --- 0. preflight: fail fast before we create anything --------------------------------------------
echo "=== 0/7 preflight ==="
for t in gcloud terraform docker kubectl python3; do
  command -v "$t" >/dev/null || { echo "✗ missing tool: $t"; exit 1; }
done
for v in EVAL_ENGINE_PG_DSN OPENROUTER_API_KEY OAUTH2_CLIENT_ID OAUTH2_CLIENT_SECRET; do
  [ -n "${!v:-}" ] || { echo "✗ missing required env: $v  (source your .env; see deploy/secrets.sh)"; exit 1; }
done
[ -f "$TFDIR/terraform.tfvars" ] || {
  echo "✗ $TFDIR/terraform.tfvars missing — copy terraform.tfvars.example and set project_id"; exit 1; }
echo "  ✓ tools + secret env + terraform.tfvars present"

# --- 1. infra: cluster + pools + IP + IAM + GCS + Artifact Registry + Helm add-ons ----------------
echo "=== 1/7 terraform apply (cluster, IP, IAM, GCS, Artifact Registry, KEDA/ingress-nginx/cert-manager) ==="
terraform -chdir="$TFDIR" init -input=false
terraform -chdir="$TFDIR" apply -auto-approve -input=false

EE_PROJECT="$(terraform -chdir="$TFDIR" output -raw project_id)"
EE_HOST="$(terraform -chdir="$TFDIR" output -raw host)"
REG="${EE_REGION}-docker.pkg.dev/${EE_PROJECT}/eval-engine"
echo "  ✓ project=${EE_PROJECT}  host=${EE_HOST}  registry=${REG}"

# --- 2. cluster credentials + docker auth for the registry ----------------------------------------
echo "=== 2/7 cluster credentials + Artifact Registry docker auth ==="
gcloud container clusters get-credentials "$EE_CLUSTER" --zone "$EE_ZONE" --project "$EE_PROJECT"
gcloud auth configure-docker "${EE_REGION}-docker.pkg.dev" --quiet

# --- 3. build + push images (the fresh AR repo is empty) ------------------------------------------
echo "=== 3/7 build + push images (app + frontend, tagged :${GIT_SHA} and :latest) ==="
DOCKER_BUILDKIT=0 docker build -f "$ROOT/deploy/Dockerfile" --build-arg GIT_SHA="$GIT_SHA" \
  -t "$REG/app:$GIT_SHA" -t "$REG/app:latest" "$ROOT"
docker push "$REG/app:$GIT_SHA"; docker push "$REG/app:latest"
DOCKER_BUILDKIT=0 docker build -f "$ROOT/frontend/Dockerfile" \
  -t "$REG/frontend:$GIT_SHA" -t "$REG/frontend:latest" "$ROOT/frontend"
docker push "$REG/frontend:$GIT_SHA"; docker push "$REG/frontend:latest"

# --- 4. secrets (from env) ------------------------------------------------------------------------
echo "=== 4/7 k8s secrets (Neon DSN, OpenRouter, OAuth, litellm, cookie) ==="
bash "$ROOT/deploy/secrets.sh"

# --- 5. render + apply manifests + dashboards + wait for rollouts ---------------------------------
echo "=== 5/7 install: render + apply manifests, dashboards, rollout wait ==="
bash "$ROOT/deploy/install.sh"

# --- 6. seed evals so the dashboard has launchable evals (best-effort) ----------------------------
echo "=== 6/7 seed evals (benchmark suite + petri_audit) ==="
(
  kubectl -n "$EE_NAMESPACE" rollout status deploy/eval-engine-api --timeout=180s >/dev/null 2>&1 || true
  kubectl -n "$EE_NAMESPACE" port-forward deploy/eval-engine-api 8080:8077 >/dev/null 2>&1 &
  PF=$!; sleep 6
  # --dataset-uri-prefix /app: the API reads the seed JSONLs from inside the image (examples/ is baked
  # in) and snapshots them to GCS server-side. --sandbox k8s: humaneval uses per-sample k8s sandboxes.
  python3 "$ROOT/tools/seed_benchmarks.py" --api http://localhost:8080 --dataset-uri-prefix /app --sandbox k8s \
    || echo "  (seeding had issues — re-run: kubectl port-forward deploy/eval-engine-api 8080:8077 then tools/seed_benchmarks.py)"
  kill "$PF" 2>/dev/null || true
) || true

# --- 7. done -------------------------------------------------------------------------------------
echo "=== 7/7 done ==="
echo "✓ system up.  Dashboard: https://${EE_HOST}"
echo ""
echo "⚠️  ONE MANUAL STEP — the ingress IP is new, so sign-in 403s until you add this to the Google"
echo "    OAuth client's Authorized redirect URIs:"
echo "        https://${EE_HOST}/oauth2/callback"
echo ""
echo "  Notes:"
echo "   • Neon Postgres is external (untouched by destroy); the control schema auto-migrates on API start."
echo "   • Workers are KEDA-scaled to 0 until a run is launched (first run waits ~1 min for scale-up)."
echo "   • Launch from the dashboard's eval drawer; for petri_audit pick an openrouter/ target (docs/PETRI.md)."
echo "   • If Grafana dashboards are missing, jsonnet/jb weren't on PATH — render later:"
echo "       export PATH=\$PATH:/usr/local/go/bin:\$HOME/go/bin && make -C deploy/grafana apply"
