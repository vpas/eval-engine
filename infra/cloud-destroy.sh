#!/usr/bin/env bash
# FULL teardown — delete EVERY billable GCP resource for eval-engine, to get the bill to ~$0.
#
# IRREVERSIBLE and DESTROYS ALL DATA: the GKE cluster, the StatefulSet PVC disks (ClickHouse / Redis /
# Grafana), the GCS bucket (.eval logs, transcripts, dataset snapshots), and every container image.
# Use this when you're DONE with the cluster — NOT for an overnight pause (that's cloud-down.sh, which
# keeps everything so cloud-up.sh resumes in minutes).
#
# Why gcloud and not `terraform destroy`: there's no local tf state in this checkout, and the
# StatefulSet PVCs + the ingress LoadBalancer's GCP resources are created dynamically by Kubernetes
# (not by Terraform), so they'd orphan after a `clusters delete` anyway. This sweeps them explicitly.
#
# NOT handled (external to GCP): **Neon Postgres** — the control-plane DB. Suspend/delete it in the
# Neon console separately (its free tier autosuspends to ~$0, so it's optional).
#
# Order: GKE cluster (nodes, pools, boot disks, cluster firewalls) → orphaned PVC disks → the ingress
# load balancer (forwarding rules, target pools, health checks) → reserved addresses → k8s/gke firewall
# rules → the Artifact Registry repo → the GCS bucket. Every step is tolerant + idempotent (safe to
# re-run if something was already gone or a delete needs a second pass).
#
# Restart later is a FROM-SCRATCH bring-up (and a NEW ingress IP → update the OAuth redirect URI):
#   cd deploy/terraform && terraform apply   →   ../secrets.sh   →   ../install.sh   →   reseed + deploy
set -uo pipefail

PROJECT="${PROJECT:-eval-engine}"
CLUSTER="${CLUSTER:-eval-engine}"
ZONE="${ZONE:-us-central1-a}"
REGION="${REGION:-us-central1}"
BUCKET="${BUCKET:-${PROJECT}-eval-engine}"          # google_storage_bucket.artifacts = "<project>-eval-engine"
AR_REPO="${AR_REPO:-eval-engine}"                   # google_artifact_registry_repository.images

# --- confirmation guard (this deletes everything + all data) -----------------------------------------
echo "⚠️  FULL TEARDOWN of GCP project '${PROJECT}'."
echo "    Deletes: cluster '${CLUSTER}', ALL persistent disks (data), the LB + IP, the Artifact Registry"
echo "    repo '${AR_REPO}', and the GCS bucket 'gs://${BUCKET}'. This is IRREVERSIBLE."
echo "    (Neon Postgres is separate — handle it in the Neon console.)"
if [ "${CONFIRM:-}" != "$PROJECT" ]; then
  if [ -t 0 ]; then
    read -r -p "    Type the project id '${PROJECT}' to confirm: " ans
  else
    ans="${CONFIRM:-}"
  fi
  [ "$ans" = "$PROJECT" ] || { echo "✗ not confirmed. Aborting. (For non-interactive: CONFIRM=${PROJECT} bash $0)"; exit 1; }
fi

run() { echo "  → $*"; "$@" || echo "    (skipped — nonzero exit, likely already gone)"; }
base() { basename "${1:-}"; }   # strip a selfLink URL down to its short name (region/zone fields)

echo ""
echo "=== 1/7  GKE cluster '${CLUSTER}' (${ZONE}) — nodes, pools, boot disks, cluster firewalls ==="
run gcloud container clusters delete "$CLUSTER" --zone "$ZONE" --project "$PROJECT" --quiet

echo "=== 2/7  leftover persistent disks (orphaned StatefulSet PVCs — your data) ==="
gcloud compute disks list --project "$PROJECT" --format="value(name,zone)" 2>/dev/null | while read -r name zone; do
  [ -n "$name" ] && run gcloud compute disks delete "$name" --zone "$(base "$zone")" --project "$PROJECT" --quiet
done

echo "=== 3/7  load-balancer resources (forwarding rules, target pools, health checks) ==="
gcloud compute forwarding-rules list --project "$PROJECT" --format="value(name,region)" 2>/dev/null | while read -r name region; do
  [ -z "$name" ] && continue
  if [ -n "$region" ]; then run gcloud compute forwarding-rules delete "$name" --region "$(base "$region")" --project "$PROJECT" --quiet
  else                       run gcloud compute forwarding-rules delete "$name" --global --project "$PROJECT" --quiet; fi
done
gcloud compute target-pools list --project "$PROJECT" --format="value(name,region)" 2>/dev/null | while read -r name region; do
  [ -n "$name" ] && run gcloud compute target-pools delete "$name" --region "$(base "$region")" --project "$PROJECT" --quiet
done
for hc in $(gcloud compute http-health-checks list --project "$PROJECT" --format="value(name)" 2>/dev/null); do
  run gcloud compute http-health-checks delete "$hc" --project "$PROJECT" --quiet
done

echo "=== 4/7  reserved IP addresses (regional + global) ==="
gcloud compute addresses list --project "$PROJECT" --format="value(name,region)" 2>/dev/null | while read -r name region; do
  [ -z "$name" ] && continue
  if [ -n "$region" ]; then run gcloud compute addresses delete "$name" --region "$(base "$region")" --project "$PROJECT" --quiet
  else                       run gcloud compute addresses delete "$name" --global --project "$PROJECT" --quiet; fi
done

echo "=== 5/7  GKE / k8s firewall rules (left behind by the cluster + LB) ==="
for fw in $(gcloud compute firewall-rules list --project "$PROJECT" --format="value(name)" 2>/dev/null | grep -E "^(k8s-|gke-${CLUSTER})"); do
  run gcloud compute firewall-rules delete "$fw" --project "$PROJECT" --quiet
done

echo "=== 6/7  Artifact Registry repo '${AR_REPO}' (${REGION}) — all images ==="
run gcloud artifacts repositories delete "$AR_REPO" --location "$REGION" --project "$PROJECT" --quiet

echo "=== 7/7  GCS bucket gs://${BUCKET} — all logs / transcripts / snapshots ==="
run gcloud storage rm --recursive "gs://${BUCKET}" --project "$PROJECT"
run gcloud storage buckets delete "gs://${BUCKET}" --project "$PROJECT" --quiet

echo ""
echo "✓ teardown complete — GCP billables for '${PROJECT}' should now be ~\$0."
echo "  • Neon Postgres is EXTERNAL — suspend/delete it in the Neon console if you're done with it."
echo "  • Verify nothing remains:"
echo "      gcloud compute instances list        --project ${PROJECT}"
echo "      gcloud compute disks list            --project ${PROJECT}"
echo "      gcloud compute forwarding-rules list --project ${PROJECT}"
echo "      gcloud compute addresses list        --project ${PROJECT}"
echo "  • Rebuild later (NEW ingress IP → update the OAuth redirect URI + nip.io host):"
echo "      cd deploy/terraform && terraform apply  →  ../secrets.sh  →  ../install.sh  →  reseed + deploy"
