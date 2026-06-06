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

variable "sandbox_max_nodes" {
  type        = number
  default     = 2
  description = "GKE-Sandbox (gVisor) pool autoscales 0..this for T2 agentic isolation. Idle = 0 nodes = $0."
}

variable "ha_stateful" {
  type        = bool
  default     = false
  description = "When true, scale the system pool to 3 nodes so the replicated ClickHouse/Redis pods can spread across nodes (anti-affinity) for HA (#16). Off = cost-minimal single node."
}
