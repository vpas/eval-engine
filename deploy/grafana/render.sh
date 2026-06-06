#!/usr/bin/env bash
# Render the grafonnet dashboards (*.jsonnet) → JSON → a single Kubernetes ConfigMap manifest.
# Output: build/<name>.json (one per dashboard) + build/grafana-dashboards.configmap.yaml.
# Apply with:  kubectl apply -f deploy/grafana/build/grafana-dashboards.configmap.yaml
#
# Tooling (install once):
#   go install github.com/google/go-jsonnet/cmd/jsonnet@latest
#   go install github.com/jsonnet-bundler/jsonnet-bundler/cmd/jb@latest
#   (ensure $(go env GOPATH)/bin is on PATH)
set -euo pipefail
cd "$(dirname "$0")"

command -v jsonnet >/dev/null || { echo "✗ jsonnet not found — see the install note at the top of render.sh"; exit 1; }
command -v jb >/dev/null      || { echo "✗ jb (jsonnet-bundler) not found — see the install note"; exit 1; }
command -v kubectl >/dev/null || { echo "✗ kubectl not found"; exit 1; }

echo "→ vendoring grafonnet (jb install)…"
jb install

rm -rf build && mkdir -p build
for f in dashboards/*.jsonnet; do
  name="$(basename "${f%.jsonnet}")"
  echo "→ rendering $name"
  jsonnet -J vendor "$f" > "build/${name}.json"
done

echo "→ building ConfigMap grafana-dashboards"
kubectl create configmap grafana-dashboards \
  --namespace eval-engine \
  --from-file=build/ \
  --dry-run=client -o yaml > build/grafana-dashboards.configmap.yaml
# label so it's clearly part of the Grafana provisioning set
kubectl label --local -f build/grafana-dashboards.configmap.yaml \
  app=grafana grafana_dashboard=1 -o yaml --dry-run=client \
  > build/.tmp && mv build/.tmp build/grafana-dashboards.configmap.yaml

echo "✓ done. Apply:  kubectl apply -f deploy/grafana/build/grafana-dashboards.configmap.yaml"
