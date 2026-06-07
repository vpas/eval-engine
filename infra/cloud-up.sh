#!/usr/bin/env bash
# Resume the GKE deployment paused by cloud-down.sh — scales the `system` pool back up, re-enables
# autoscaling on the `workers` + `sandbox` pools (which cloud-down disabled to keep them pinned at 0),
# and waits for the workloads to reschedule (Deployments persist across the pause, so no redeploy).
set -euo pipefail

CLUSTER="${CLUSTER:-eval-engine}"
ZONE="${ZONE:-us-central1-a}"
SYSTEM_POOL="${SYSTEM_POOL:-system}"
NAMESPACE="${NAMESPACE:-eval-engine}"
HOST="${HOST:-https://35-202-212-111.nip.io}"
# System node count to resume to. Default 1 (cost-minimal); set SYSTEM_NODES=3 to restore the HA
# stateful footprint (replicated ClickHouse/Redis spread across nodes via anti-affinity — ha_stateful).
SYSTEM_NODES="${SYSTEM_NODES:-1}"
# Autoscaling bounds to RESTORE on the pools cloud-down disabled — mirror deploy/terraform (min 0).
WORKER_MAX_NODES="${WORKER_MAX_NODES:-3}"
SANDBOX_MAX_NODES="${SANDBOX_MAX_NODES:-2}"

echo "Resuming cluster '${CLUSTER}' (${ZONE}) — scaling '${SYSTEM_POOL}' pool to ${SYSTEM_NODES}…"
gcloud container clusters resize "${CLUSTER}" \
  --node-pool "${SYSTEM_POOL}" --num-nodes "${SYSTEM_NODES}" --zone "${ZONE}" --quiet

# Re-enable autoscaling on the pools cloud-down.sh disabled, so worker/agentic load scales them up on
# demand again (and they idle back to 0). Idempotent — a no-op if autoscaling is already enabled.
echo "Re-enabling autoscaling: workers 0..${WORKER_MAX_NODES}, sandbox 0..${SANDBOX_MAX_NODES}…"
gcloud container node-pools update workers \
  --cluster "${CLUSTER}" --zone "${ZONE}" \
  --enable-autoscaling --min-nodes 0 --max-nodes "${WORKER_MAX_NODES}" --quiet
gcloud container node-pools update sandbox \
  --cluster "${CLUSTER}" --zone "${ZONE}" \
  --enable-autoscaling --min-nodes 0 --max-nodes "${SANDBOX_MAX_NODES}" --quiet

echo "Waiting for a node to register…"
until [ "$(kubectl get nodes -l cloud.google.com/gke-nodepool="${SYSTEM_POOL}" \
            -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
            2>/dev/null | grep -c True)" -ge 1 ]; do sleep 5; done
echo "✓ node Ready. Waiting for core deployments to roll out…"

for d in eval-engine-api eval-engine-frontend inspect-view litellm; do
  kubectl -n "${NAMESPACE}" rollout status "deploy/${d}" --timeout=180s 2>/dev/null \
    || echo "  (note: ${d} not ready yet — check 'kubectl -n ${NAMESPACE} get pods')"
done

echo "✓ resumed. Dashboard: ${HOST}"
