variable "project_id" {
  type        = string
  description = "Your GCP project id (e.g. eval-engine-vp01)."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type        = string
  default     = "us-central1-a"
  description = "ZONAL cluster → one zonal cluster's control-plane fee is free under the GKE credit."
}

variable "cluster_name" {
  type    = string
  default = "eval-engine"
}

variable "system_machine_type" {
  type        = string
  default     = "e2-standard-4" # 4 vCPU / 16GB — always-on pool: API, Orchestrator, LiteLLM, ClickHouse, Redis, KEDA
  description = "Always-on node. e2-medium (2 vCPU) is too small: GKE system daemons reserve ~640m of ~940m allocatable CPU, leaving no room for the always-on stack (~500m). e2-standard-4 (~3200m free) holds the whole stack with no trimming."
}

variable "worker_machine_type" {
  type    = string
  default = "e2-medium"
}

variable "worker_max_nodes" {
  type        = number
  default     = 3
  description = "Spot worker pool autoscales 0..this. Idle = 0 nodes = $0."
}
