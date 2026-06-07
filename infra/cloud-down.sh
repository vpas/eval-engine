#!/usr/bin/env bash
# Pause the GKE deployment to stop most compute billing overnight — reversible (state persists).
#
# Keeps ONE small `system` node and scales the autoscaling `workers` + `sandbox` pools to 0. Keeps the
# cluster, PVCs, config, static IP and images — so `cloud-up.sh` brings it all back in a few minutes
# with no redeploy.
#
# Why we keep one system node instead of scaling it to 0 (this is the fix for a leftover billed node):
# a GKE Standard cluster won't sit at literally 0 nodes while it exists — critical control-plane addons
# that are *Deployments*, not DaemonSets (notably `konnectivity-agent`) must run somewhere. With the
# system pool at 0 they become unschedulable and GKE's cluster autoscaler resurrects an autoscaling-pool
# node to host them — and it picked the *expensive gVisor `sandbox` pool* (observed: `TriggeredScaleUp
# ... sandbox ... 0->1`), leaving a billed node up after the "pause". Keeping one small `system` node
# gives those addons a cheap home; the autoscaling pools then idle to 0 on their own (and still scale
# up on demand for real work). App pods that don't fit on the one node simply stay Pending — harmless,
# and they can't pull up the tainted workers/sandbox pools (they don't tolerate those taints).
#
# NOT paused (intentionally, all negligible and needed for a clean restore): the GKE control-plane
# management fee (~$0.10/hr), the ingress load balancer + its static IP (deleting it would change
# 35-202-212-111.nip.io and break the OAuth redirect URIs), and persistent disks (your data). For true
# $0 compute you'd `terraform destroy` the cluster — a full teardown, not an overnight pause.
#
# This targets the LOCAL-dev backends' cloud counterpart; for the local Postgres+ClickHouse used by
# the test suite see up.sh / down.sh instead.
set -euo pipefail

CLUSTER="${CLUSTER:-eval-engine}"
ZONE="${ZONE:-us-central1-a}"
SYSTEM_POOL="${SYSTEM_POOL:-system}"
# System nodes to keep while paused. MUST be >= 1 (see header: 0 makes konnectivity-agent homeless and
# the autoscaler resurrects an expensive sandbox node). 1 is enough to host the critical addons.
PAUSE_SYSTEM_NODES="${PAUSE_SYSTEM_NODES:-1}"
# Autoscaling pools (min 0) to drain to 0. They keep autoscaling enabled — with the system node hosting
# the addons there's no pending demand, so the autoscaler holds them at 0 (and scales up on demand).
AUTOSCALED_POOLS=(${AUTOSCALED_POOLS:-workers sandbox})

echo "Pausing cluster '${CLUSTER}' (${ZONE}) — system→${PAUSE_SYSTEM_NODES}, ${AUTOSCALED_POOLS[*]}→0…"

for pool in "${AUTOSCALED_POOLS[@]}"; do
  echo "  scaling '${pool}' → 0…"
  gcloud container clusters resize "${CLUSTER}" \
    --node-pool "${pool}" --num-nodes 0 --zone "${ZONE}" --quiet
done

echo "  scaling '${SYSTEM_POOL}' → ${PAUSE_SYSTEM_NODES} (keeps the addons' home)…"
gcloud container clusters resize "${CLUSTER}" \
  --node-pool "${SYSTEM_POOL}" --num-nodes "${PAUSE_SYSTEM_NODES}" --zone "${ZONE}" --quiet

echo "✓ paused (system=${PAUSE_SYSTEM_NODES}, workers/sandbox=0). Resume with: infra/cloud-up.sh"
