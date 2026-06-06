# Cluster add-ons as code: KEDA (worker autoscaler), ingress-nginx (pinned to the reserved IP),
# cert-manager (TLS for the OIDC host). Previously manual `helm install`s — now `terraform apply`.
#
# Provider bootstrap: the helm/kubernetes providers authenticate to the cluster THIS config is
# creating, via a short-lived token from google_client_config. This is the standard single-apply
# pattern; provider config is lazy, so it resolves after the cluster exists. The releases also
# depend_on the system node pool so there's a node to schedule onto.
data "google_client_config" "default" {}

provider "helm" {
  kubernetes {
    host                   = "https://${google_container_cluster.primary.endpoint}"
    token                  = data.google_client_config.default.access_token
    cluster_ca_certificate = base64decode(google_container_cluster.primary.master_auth[0].cluster_ca_certificate)
  }
}

provider "kubernetes" {
  host                   = "https://${google_container_cluster.primary.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.primary.master_auth[0].cluster_ca_certificate)
}

# Worker autoscaler. The ScaledObject + TriggerAuthentication themselves stay app manifests
# (deploy/k8s/70-keda.yaml) — this just installs the operator + CRDs.
resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.keda_chart_version
  namespace        = "keda"
  create_namespace = true
  depends_on       = [google_container_node_pool.system]
}

# Ingress controller, pinned to the reserved static IP so the LB address (and the nip.io host) is
# deterministic and stable across rebuilds.
resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  version          = var.ingress_nginx_chart_version
  namespace        = "ingress-nginx"
  create_namespace = true

  set {
    name  = "controller.service.loadBalancerIP"
    value = google_compute_address.ingress.address
  }
  # OIDC cookies/headers are large; match 62-ingress.yaml's proxy-buffer-size at the controller.
  set {
    name  = "controller.config.proxy-buffer-size"
    value = "16k"
  }

  depends_on = [google_container_node_pool.system, google_compute_address.ingress]
}

# TLS via Let's Encrypt. installCRDs so the ClusterIssuer (deploy/k8s/60-cert-issuer.yaml) applies.
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.cert_manager_chart_version
  namespace        = "cert-manager"
  create_namespace = true

  set {
    name  = "installCRDs"
    value = "true"
  }

  depends_on = [google_container_node_pool.system]
}
