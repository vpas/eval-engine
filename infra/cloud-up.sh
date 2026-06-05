#!/usr/bin/env bash
# Resume the GKE deployment paused by cloud-down.sh — scales the `system` pool back up and waits
# for the workloads to reschedule (Deployments persist across the pause, so there is no redeploy).
set -euo pipefail

CLUSTER="${CLUSTER:-eval-engine}"
ZONE="${ZONE:-us-central1-a}"
SYSTEM_POOL="${SYSTEM_POOL:-system}"
NAMESPACE="${NAMESPACE:-eval-engine}"
HOST="${HOST:-https://35-202-212-111.nip.io}"

echo "Resuming cluster '${CLUSTER}' (${ZONE}) — scaling '${SYSTEM_POOL}' pool to 1…"
gcloud container clusters resize "${CLUSTER}" \
  --node-pool "${SYSTEM_POOL}" --num-nodes 1 --zone "${ZONE}" --quiet

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
