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
# 35-202-212-111.nip.io and break the OAuth redirect URIs), and persistent disks (your data).
#
# DEEP STOP (`--full`, or FULL_STOP=1): scale EVERYTHING to 0, including the system pool — the lowest
# bill short of a teardown (only the control-plane fee + LB/IP + disks remain). To stop the autoscaler
# from resurrecting an expensive node for the homeless addons (the problem above), `--full` first
# DISABLES autoscaling on the workers/sandbox pools so nothing can scale up; the control-plane addons
# then just sit Pending until you resume. `cloud-up.sh` re-enables autoscaling + restores the system
# pool, so it self-heals — no extra step. For literally $0 you'd still `terraform destroy` the cluster.
#
# This targets the LOCAL-dev backends' cloud counterpart; for the local Postgres+ClickHouse used by
# the test suite see up.sh / down.sh instead.
set -euo pipefail

# --full / -f / FULL_STOP=1 → deep stop (system pool to 0 too; see header).
FULL_STOP="${FULL_STOP:-0}"
for arg in "$@"; do
  case "$arg" in
    --full|--all|--zero|-f) FULL_STOP=1 ;;
    -h|--help) echo "usage: $(basename "$0") [--full]   (--full also drains the system pool → 0 nodes)"; exit 0 ;;
    *) echo "unknown option: $arg (use --full for a deep stop, or -h)"; exit 2 ;;
  esac
done

CLUSTER="${CLUSTER:-eval-engine}"
ZONE="${ZONE:-us-central1-a}"
SYSTEM_POOL="${SYSTEM_POOL:-system}"
# System nodes to keep while paused. Default 1 — hosts the critical addons cheaply (see header: 0 makes
# konnectivity-agent homeless). `--full` sets 0 (and disables the autoscaling pools so nothing resurrects).
if [ "$FULL_STOP" = 1 ]; then
  PAUSE_SYSTEM_NODES="${PAUSE_SYSTEM_NODES:-0}"
else
  PAUSE_SYSTEM_NODES="${PAUSE_SYSTEM_NODES:-1}"
fi
# Autoscaling pools (min 0) to drain to 0. Default pause leaves autoscaling ON (the system node hosts the
# addons, so there's no pending demand). `--full` turns autoscaling OFF first, so the autoscaler can't
# bring a node back to host the homeless addons.
AUTOSCALED_POOLS=(${AUTOSCALED_POOLS:-workers sandbox})

if [ "$FULL_STOP" = 1 ]; then
  echo "DEEP-pausing cluster '${CLUSTER}' (${ZONE}) — ${AUTOSCALED_POOLS[*]} + ${SYSTEM_POOL} → 0 (everything)…"
else
  echo "Pausing cluster '${CLUSTER}' (${ZONE}) — system→${PAUSE_SYSTEM_NODES}, ${AUTOSCALED_POOLS[*]}→0…"
fi

for pool in "${AUTOSCALED_POOLS[@]}"; do
  if [ "$FULL_STOP" = 1 ]; then
    echo "  disabling autoscaling on '${pool}' (so it can't be resurrected to host homeless addons)…"
    gcloud container node-pools update "${pool}" \
      --cluster "${CLUSTER}" --zone "${ZONE}" --no-enable-autoscaling --quiet
  fi
  echo "  scaling '${pool}' → 0…"
  gcloud container clusters resize "${CLUSTER}" \
    --node-pool "${pool}" --num-nodes 0 --zone "${ZONE}" --quiet
done

echo "  scaling '${SYSTEM_POOL}' → ${PAUSE_SYSTEM_NODES}…"
gcloud container clusters resize "${CLUSTER}" \
  --node-pool "${SYSTEM_POOL}" --num-nodes "${PAUSE_SYSTEM_NODES}" --zone "${ZONE}" --quiet

if [ "$FULL_STOP" = 1 ]; then
  echo "✓ DEEP-paused — all pools at 0 nodes (control-plane addons will sit Pending; that's expected)."
  echo "  Only the GKE control-plane fee + load balancer/IP + persistent disks still bill."
  echo "  Resume with: infra/cloud-up.sh  (it re-enables autoscaling + restores the system pool)."
else
  echo "✓ paused (system=${PAUSE_SYSTEM_NODES}, workers/sandbox=0). Resume with: infra/cloud-up.sh"
fi
