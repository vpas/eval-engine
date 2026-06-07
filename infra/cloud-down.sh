#!/usr/bin/env bash
# Pause the GKE deployment to stop compute billing overnight — reversible (state persists).
#
# Scales ALL three node pools to 0: the fixed `system` pool (api, frontend, inspect-view, clickhouse,
# litellm, orchestrator, ingress-nginx, oauth2-proxy) and the autoscaling `workers` + `sandbox` pools.
# Keeps the cluster, PVCs, config, static IP and images — so `cloud-up.sh` brings it all back in a few
# minutes with no redeploy.
#
# Why the autoscaling pools need explicit handling (this used to leave a billed node up): scaling only
# `system` to 0 evicts GKE-managed control-plane addons that are *Deployments*, not DaemonSets — most
# notably `konnectivity-agent`. With no `system` node left to host them and the `workers`/`sandbox`
# pools still autoscaling-enabled, the cluster autoscaler scales one of those pools 0->1 to place the
# homeless addon (observed: `TriggeredScaleUp ... sandbox ... 0->1`), so a node — and its bill — stays
# up after the "pause". You can't reach 0 while autoscaling is on (the autoscaler just re-creates the
# node), so we DISABLE autoscaling on those pools first; cloud-up.sh re-enables it with the original
# bounds. With every pool at 0, the addons sit harmlessly Pending until resume.
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
# The autoscaling pools (min 0) that must ALSO be driven to 0 with autoscaling disabled so the resize
# sticks. Names match deploy/terraform/main.tf; cloud-up.sh re-enables autoscaling on these.
AUTOSCALED_POOLS=(${AUTOSCALED_POOLS:-workers sandbox})

echo "Pausing cluster '${CLUSTER}' (${ZONE}) — scaling all pools to 0…"

# 1) Disable autoscaling on the autoscaling pools FIRST, so the autoscaler can't resurrect a node for
#    a homeless addon (konnectivity-agent) the moment the system pool hits 0 (see header note).
for pool in "${AUTOSCALED_POOLS[@]}"; do
  echo "  disabling autoscaling on '${pool}'…"
  gcloud container node-pools update "${pool}" \
    --cluster "${CLUSTER}" --zone "${ZONE}" --no-enable-autoscaling --quiet
done

# 2) Scale every pool to 0.
for pool in "${SYSTEM_POOL}" "${AUTOSCALED_POOLS[@]}"; do
  echo "  scaling '${pool}' → 0…"
  gcloud container clusters resize "${CLUSTER}" \
    --node-pool "${pool}" --num-nodes 0 --zone "${ZONE}" --quiet
done

echo "✓ paused (all pools at 0). Compute billing stopped. Resume with: infra/cloud-up.sh"
