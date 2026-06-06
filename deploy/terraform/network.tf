# Reserved external IP for the ingress LB. Reserving it in Terraform (rather than letting the LB
# allocate an ephemeral one) is the key to painless cluster migration: the IP — and therefore the
# nip.io host derived from it — is known BEFORE anything is deployed and is STABLE across cluster
# rebuilds. That means the Google OAuth redirect URI is registered once, ever, and a migration never
# touches OAuth. ingress-nginx is pinned to this IP in helm.tf; the host is exposed in outputs.tf and
# consumed by deploy/install.sh (envsubst into the ingress / oauth2-proxy / grafana manifests).
resource "google_compute_address" "ingress" {
  name   = "${var.cluster_name}-ingress"
  region = var.region

  depends_on = [google_project_service.apis]
}
