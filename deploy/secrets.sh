#!/usr/bin/env bash
# Create/refresh the k8s secrets the stack mounts. Values come from env (never committed); run before
# install.sh. Idempotent (apply). Secret VALUES are never echoed, but note they transit argv — run on
# a trusted host. For production, source these from Secret Manager instead.
#
# Required env:
#   EVAL_ENGINE_PG_DSN        Neon Postgres DSN (postgresql://user:pass@host/db?sslmode=require)
#   OPENROUTER_API_KEY        OpenRouter key (the gateway's upstream)
#   OAUTH2_CLIENT_ID          Google OAuth web client id
#   OAUTH2_CLIENT_SECRET      Google OAuth web client secret
# Optional (auto-generated if unset):
#   LITELLM_MASTER_KEY        gateway master key            (default: sk-<random>)
#   OAUTH2_COOKIE_SECRET      oauth2-proxy cookie secret    (default: random 32-byte base64url)
#   GRAFANA_ADMIN_PASSWORD    Grafana break-glass admin     (secret skipped if unset)
set -euo pipefail
NS="${EE_NAMESPACE:-eval-engine}"

need() { [ -n "${!1:-}" ] || { echo "✗ missing required env: $1"; exit 1; }; }
need EVAL_ENGINE_PG_DSN; need OPENROUTER_API_KEY; need OAUTH2_CLIENT_ID; need OAUTH2_CLIENT_SECRET

gen() { python3 -c "import secrets,base64,sys; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"; }
LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-$(python3 -c 'import secrets;print(secrets.token_hex(24))')}"
OAUTH2_COOKIE_SECRET="${OAUTH2_COOKIE_SECRET:-$(gen)}"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

mk() { # mk <secret-name> <k=v>...
  local name="$1"; shift
  local args=(); for kv in "$@"; do args+=(--from-literal="$kv"); done
  kubectl create secret generic "$name" -n "$NS" "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "   ✓ secret/$name"
}

echo "→ secrets in namespace $NS"
mk eval-pg     "dsn=$EVAL_ENGINE_PG_DSN"
mk openrouter  "key=$OPENROUTER_API_KEY"
mk litellm     "master_key=$LITELLM_MASTER_KEY"
mk oauth2-proxy "client-id=$OAUTH2_CLIENT_ID" "client-secret=$OAUTH2_CLIENT_SECRET" "cookie-secret=$OAUTH2_COOKIE_SECRET"
[ -n "${GRAFANA_ADMIN_PASSWORD:-}" ] && mk grafana "admin-password=$GRAFANA_ADMIN_PASSWORD" || \
  echo "   • grafana admin-password skipped (auth-proxy is the normal path)"

echo "✓ done. Next: ../install.sh"
