#!/usr/bin/env bash
# Pause the GKE deployment to stop compute billing overnight — reversible (state persists).
#
# Scales the fixed `system` node pool to 0. That evicts every pod (api, frontend, inspect-view,
# clickhouse, litellm, orchestrator, ingress-nginx, oauth2-proxy) but keeps the cluster, PVCs,
# config, static IP and images — so `cloud-up.sh` brings it all back in a few minutes with no
# redeploy. The `workers` pool already autoscales to 0 when idle, so nothing to do there.
#
# NOT paused (intentionally, all negligible and needed for a clean restore): the GKE control-plane
# management fee (~$0.10/hr), the ingress load balancer + its static IP (deleting it would change
# 35-202-212-111.nip.io and break the OAuth redirect URIs), and persistent disks (your data).
# To stop *those* too you'd `terraform destroy` the cluster — a full teardown, not an overnight pause.
#
# This targets the LOCAL-dev backends' cloud counterpart; for the local Postgres+ClickHouse used by
# the test suite see up.sh / down.sh instead.
set -euo pipefail

CLUSTER="${CLUSTER:-eval-engine}"
ZONE="${ZONE:-us-central1-a}"
SYSTEM_POOL="${SYSTEM_POOL:-system}"

echo "Pausing cluster '${CLUSTER}' (${ZONE}) — scaling '${SYSTEM_POOL}' pool to 0…"
gcloud container clusters resize "${CLUSTER}" \
  --node-pool "${SYSTEM_POOL}" --num-nodes 0 --zone "${ZONE}" --quiet

echo "✓ paused. Compute billing stopped. Resume with: infra/cloud-up.sh"
