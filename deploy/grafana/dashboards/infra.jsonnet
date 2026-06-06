// Infra — the cost & reliability substrate. Source: Google Managed Prometheus (GMP) frontend.
//
// REQUIRES GMP managed collection for kube-state-metrics + kubelet/cadvisor to be enabled on the
// cluster (see docs/OBSERVABILITY.md §GMP). The headline panel — worker replica count over time —
// is what makes KEDA's 0→N scaling visible; overlay it on the Fleet dashboard's queue depth to see
// the autoscaler reacting.
local ee = import '../lib/ee.libsonnet';
local p = ee.dsProm;

local nsSel = 'namespace="eval-engine"';

ee.dashboard('Eval Engine — Infra', 'ee-infra', [
  ee.row('Autoscaling & topology'),
  ee.timeseries('Worker replicas (KEDA 0→N)', p, [
    ee.prom('kube_deployment_status_replicas{' + nsSel + ', deployment="eval-engine-worker"}', 'desired', 'A'),
    ee.prom('kube_deployment_status_replicas_available{' + nsSel + ', deployment="eval-engine-worker"}', 'available', 'B'),
  ]),
  ee.timeseries('Cluster nodes (system + spot)', p, [ee.prom(
    'count(kube_node_info) by (node)', '{{node}}',
  )]),

  ee.row('Workload readiness'),
  ee.table('Ready vs desired (all deployments)', p, [ee.prom(
    'kube_deployment_status_replicas_available{' + nsSel + '} / kube_deployment_status_replicas{' + nsSel + '}', '{{deployment}}',
  )], 'percentunit'),
  ee.timeseries('Pod restarts (rate)', p, [ee.prom(
    'sum(rate(kube_pod_container_status_restarts_total{' + nsSel + '}[$__rate_interval])) by (pod)', '{{pod}}',
  )]),

  ee.row('Resource usage'),
  // LiteLLM was OOMKilled at 1Gi (now 2.5Gi); ClickHouse is the other memory whale. Watch both
  // against limits on the always-on e2-standard-4.
  ee.timeseries('Memory working set by pod', p, [ee.prom(
    'sum(container_memory_working_set_bytes{' + nsSel + ', container!=""}) by (pod)', '{{pod}}',
  )], 'bytes'),
  ee.timeseries('CPU usage by pod', p, [ee.prom(
    'sum(rate(container_cpu_usage_seconds_total{' + nsSel + ', container!=""}[$__rate_interval])) by (pod)', '{{pod}}',
  )]),

  ee.row('Storage'),
  ee.timeseries('PVC usage (ClickHouse / Redis)', p, [ee.prom(
    'kubelet_volume_stats_used_bytes{' + nsSel + '} / kubelet_volume_stats_capacity_bytes{' + nsSel + '}', '{{persistentvolumeclaim}}',
  )], 'percentunit'),
])
