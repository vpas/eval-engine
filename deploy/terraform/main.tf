# Cheapest persistent GKE Standard: one ZONAL cluster (free control plane), a single always-on
# node for the control/data plane, and a SPOT worker pool that autoscales to ZERO when idle.
# No Cloud NAT (public nodes) and no LoadBalancer (we port-forward) — the two silent $18–32/mo costs.

# Ensure the required APIs are on (idempotent; safe if you already enabled them in Phase 0).
resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.zone # ZONAL (not regional) → free control plane

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false # so `terraform destroy` actually works

  # Default logging/monitoring left on — a tiny cluster sits inside Cloud Logging's free 50 GiB/mo.
  release_channel { channel = "REGULAR" }

  # default VPC, PUBLIC nodes → no Cloud NAT needed (nodes reach OpenRouter/Artifact Registry directly)
  depends_on = [google_project_service.apis]
}

# Always-on pool: hosts the control plane + Orchestrator + LiteLLM + ClickHouse/Redis + KEDA operator.
resource "google_container_node_pool" "system" {
  name       = "system"
  location   = var.zone
  cluster    = google_container_cluster.primary.name
  node_count = 1

  node_config {
    machine_type = var.system_machine_type
    disk_type    = "pd-standard"
    disk_size_gb = 30
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"] # node SA → pull images, write GCS (test-grade; tighten with Workload Identity later)
    labels       = { "eval-engine/role" = "system" }
  }
}

# Worker pool: SPOT + autoscale 0..N. Tainted so ONLY eval-engine workers (with the matching
# toleration) land here and the pool can scale to zero. Spot eviction is a non-event — lease-reclaim
# handles it. KEDA scales the worker Deployment on ledger queue depth; the pool autoscaler follows.
resource "google_container_node_pool" "workers" {
  name     = "workers"
  location = var.zone
  cluster  = google_container_cluster.primary.name

  autoscaling {
    min_node_count = 0
    max_node_count = var.worker_max_nodes
  }

  node_config {
    machine_type = var.worker_machine_type
    spot         = true
    disk_type    = "pd-standard"
    disk_size_gb = 30
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    labels       = { "eval-engine/role" = "worker" }

    taint {
      key    = "eval-engine/worker"
      value  = "true"
      effect = "NO_SCHEDULE"
    }
  }
}

# Object store for .eval logs + transcripts (the S3-API tier). Pennies at test volume.
resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-eval-engine"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
}

# Docker registry for the eval-engine image.
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "eval-engine"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}
