#!/usr/bin/env bash
# Idempotent app-layer bring-up for a (new or existing) cluster. Run AFTER `terraform apply`
# (cluster + pools + IP + IAM + KEDA/ingress-nginx/cert-manager) and AFTER deploy/secrets.sh.
#
#   cd deploy/terraform && terraform apply        # infra + add-ons + reserved IP/IAM
#   ../secrets.sh                                  # the ~5 k8s secrets (from env)
#   ../install.sh                                  # this: render + apply manifests + dashboards
#
# Cluster-specific values come from `terraform output` (override with EE_* env if not using TF here).
# Re-running is safe: every step is `kubectl apply` (the schema-init Job is recreated explicitly).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
K8S="$ROOT/deploy/k8s"
TFDIR="$ROOT/deploy/terraform"
tf() { terraform -chdir="$TFDIR" output -raw "$1" 2>/dev/null || true; }

EE_HOST="${EE_HOST:-$(tf host)}"
EE_PROJECT="${EE_PROJECT:-$(tf project_id)}"
EE_CLUSTER="${EE_CLUSTER:-eval-engine}"
EE_ZONE="${EE_ZONE:-us-central1-a}"
EE_NAMESPACE="${EE_NAMESPACE:-eval-engine}"
export EE_HOST EE_PROJECT EE_CLUSTER EE_ZONE EE_NAMESPACE

[ -n "$EE_HOST" ]    || { echo "✗ EE_HOST unresolved — run 'terraform apply' first or export EE_HOST"; exit 1; }
[ -n "$EE_PROJECT" ] || { echo "✗ EE_PROJECT unresolved — export EE_PROJECT or set it in terraform"; exit 1; }

echo "→ cluster=$EE_CLUSTER zone=$EE_ZONE project=$EE_PROJECT host=$EE_HOST"
gcloud container clusters get-credentials "$EE_CLUSTER" --zone "$EE_ZONE" --project "$EE_PROJECT"

# Files carrying ${EE_*} placeholders → render with envsubst (explicit var list so shell-style
# ${HOSTNAME##*-} in the redis/clickhouse manifests is left untouched).
TEMPLATED=" 40-control-plane.yaml 61-oauth2-proxy.yaml 62-ingress.yaml 95-grafana.yaml "
SUBST_VARS='${EE_HOST} ${EE_PROJECT} ${EE_CLUSTER} ${EE_ZONE} ${EE_NAMESPACE}'

apply_one() {
  local f="$1" path="$K8S/$1"
  if [[ "$f" == *"20-schema-init-job.yaml" ]]; then
    # Jobs are immutable; delete (by name, from the file) then recreate so a re-run re-inits cleanly.
    kubectl delete -f "$path" --ignore-not-found >/dev/null 2>&1 || true
    kubectl apply -f "$path"
  elif [[ "$TEMPLATED" == *" $f "* ]]; then
    envsubst "$SUBST_VARS" < "$path" | kubectl apply -f -
  else
    kubectl apply -f "$path"
  fi
}

echo "→ applying manifests (deploy/manifests.txt order)"
grep -vE '^\s*#|^\s*$' "$ROOT/deploy/manifests.txt" | awk '{print $1}' | while read -r f; do
  echo "   • $f"
  apply_one "$f"
done

echo "→ rendering + applying Grafana dashboards"
if command -v jsonnet >/dev/null && command -v jb >/dev/null; then
  make -C "$ROOT/deploy/grafana" apply
else
  echo "   ! jsonnet/jb not on PATH — skipping dashboards. Render later with:"
  echo "     export PATH=\$PATH:/usr/local/go/bin:\$HOME/go/bin && make -C deploy/grafana apply"
fi

# LiteLLM only re-reads its ConfigMap on restart (no-op on a fresh install).
kubectl rollout restart deploy/litellm -n "$EE_NAMESPACE" >/dev/null 2>&1 || true

echo "→ waiting for core rollouts"
for d in eval-engine-api eval-engine-orch litellm eval-engine-frontend grafana; do
  kubectl rollout status "deploy/$d" -n "$EE_NAMESPACE" --timeout=180s || true
done

echo "✓ done.  https://$EE_HOST   (Grafana: https://$EE_HOST/grafana)"
echo "  Reminder: the Google OAuth client's redirect URI must include https://$EE_HOST/oauth2/callback"
echo "  (stable because the LB IP is reserved in Terraform — register once)."
