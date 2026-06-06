output "get_credentials" {
  description = "Run this to point kubectl at the new cluster."
  value       = "gcloud container clusters get-credentials ${var.cluster_name} --zone ${var.zone} --project ${var.project_id}"
}

output "registry_host" {
  description = "Docker registry hostname for tagging/pushing the image."
  value       = "${var.region}-docker.pkg.dev"
}

output "image_repo" {
  description = "Full image repo prefix — tag the image as <this>/eval-engine:<tag>."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/eval-engine"
}

output "bucket" {
  description = "GCS bucket for logs/transcripts (set EVAL_ENGINE_S3_BUCKET / artifact path to this)."
  value       = google_storage_bucket.artifacts.name
}

output "lb_ip" {
  description = "Reserved external IP for the ingress LB (stable across cluster rebuilds)."
  value       = google_compute_address.ingress.address
}

# The nip.io host derived from the reserved IP. deploy/install.sh envsubst's this (EE_HOST) into the
# ingress / oauth2-proxy / grafana manifests. Register it once as the Google OAuth redirect host.
output "host" {
  description = "Public host (nip.io). Consumed by deploy/install.sh as EE_HOST."
  value       = "${replace(google_compute_address.ingress.address, ".", "-")}.nip.io"
}

output "project_id" {
  description = "GCP project id. Consumed by deploy/install.sh as EE_PROJECT."
  value       = var.project_id
}
