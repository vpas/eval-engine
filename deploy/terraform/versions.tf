terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    # GKE Sandbox (gVisor) node-pool config (node_config.sandbox_config) is google-beta-only as of
    # provider v6 — used solely by the `sandbox` node pool (#18).
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
    # In-cluster add-ons (KEDA, ingress-nginx, cert-manager) + the IAM/IP they need are declared in
    # helm.tf / iam.tf / network.tf so a new cluster is `terraform apply` + the two deploy scripts.
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}
