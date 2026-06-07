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

## Panel sizing (info density)

Dashboards lay out with grafonnet `wrapPanels`, which **honours each panel's own size** and wraps to
a new line at the 24-column grid (empty `ee.row(...)` panels are section breaks). The constructors in
`lib/ee.libsonnet` bake in sensible default footprints, so most dashboards need no manual `gridPos`:

| Helper | Default `w×h` | Use |
|---|---|---|
| `ee.stat` | `6×4` | one compact KPI; four sit in a row as a strip |
| `ee.timeseries` / `ee.barchart` | `12×8` | two per row |
| `ee.table` | `24×8` | full width so many-column rows aren't truncated |

Override any single panel with `ee.size(panel, w, h)` — e.g. pack a table (`16`) next to a bar chart
(`8`) on one line. KPI clusters are a strip of `stat`s (the panel title labels each).

## Access

Behind the existing OAuth gate at **https://35-202-212-111.nip.io/grafana** — no separate login
(Grafana trusts the `X-Forwarded-Email` oauth2-proxy forwards). Folder: **Eval Engine**.
