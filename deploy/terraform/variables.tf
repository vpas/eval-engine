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
  default     = "e2-medium" # 2 vCPU / 4GB — always-on pool: API, gateway, ClickHouse, Redis, Ray head, operator
  description = "Always-on node. e2-small (~$12/mo) is cheaper but tight once ClickHouse is on it; e2-medium (~$24/mo) is the safe minimal."
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
