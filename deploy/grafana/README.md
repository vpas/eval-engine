# Grafana dashboards (as code)

Grafonnet sources → rendered JSON → a Kubernetes ConfigMap that Grafana auto-loads. No clicking in
the Grafana UI: datasources and dashboards are both provisioned. Design rationale + the full metric
catalogue live in [`docs/OBSERVABILITY.md`](../../docs/OBSERVABILITY.md).

## Layout

```
lib/ee.libsonnet          shared helpers (datasource refs, panel/target/dashboard constructors)
dashboards/*.jsonnet      one file per dashboard (quality, cost, fleet, gateway, infra)
jsonnetfile.json          grafonnet dependency (jsonnet-bundler)
render.sh / Makefile       render → build/*.json + build/grafana-dashboards.configmap.yaml
```

## Prerequisites (once)

```bash
go install github.com/google/go-jsonnet/cmd/jsonnet@latest
go install github.com/jsonnet-bundler/jsonnet-bundler/cmd/jb@latest
export PATH="$PATH:$(go env GOPATH)/bin"
```

## Edit → render → deploy

```bash
cd deploy/grafana
make render        # vendors grafonnet, renders JSON, builds the ConfigMap manifest
make apply         # kubectl apply the dashboards ConfigMap
make deploy        # apply dashboards + (re)apply the Grafana stack (k8s/95-grafana.yaml)
```

Add a dashboard: drop `dashboards/<name>.jsonnet` (copy an existing one), `make apply`. The file
provider picks it up within ~30s; Grafana need not restart.

## Access

Behind the existing OAuth gate at **https://35-202-212-111.nip.io/grafana** — no separate login
(Grafana trusts the `X-Auth-Request-Email` oauth2-proxy forwards). Folder: **Eval Engine**.
