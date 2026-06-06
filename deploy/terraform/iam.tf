# IAM the cluster needs at runtime, declared instead of run as ad-hoc `gcloud` commands.
#
# Nodes run as the project's DEFAULT compute service account (node_config has no custom SA; the
# cloud-platform oauth scope gates by IAM). These bindings grant exactly what the workloads read:
#   - monitoring.viewer  → the GMP query frontend reads Managed Prometheus (Grafana infra/gateway dashboards)
#   - storage.objectAdmin → workers write .eval logs + transcripts to the artifacts bucket (M9)
# On a default-SA cluster these may be subsumed by the SA's broad Editor role, but declaring them
# explicitly is correct and survives tightening the node SA later (Workload Identity).
data "google_project" "this" {}

locals {
  node_sa = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

resource "google_project_iam_member" "node_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = local.node_sa
}

resource "google_storage_bucket_iam_member" "node_bucket_admin" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = local.node_sa
}
