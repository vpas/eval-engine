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
