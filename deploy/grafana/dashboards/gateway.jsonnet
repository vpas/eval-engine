// Model Gateway — LiteLLM throughput / latency / spend / errors. Source: LiteLLM's built-in
// Prometheus exporter (enabled in deploy/k8s/30-litellm.yaml), scraped by Google Managed
// Prometheus and queried through the GMP frontend datasource.
//
// NOTE: LiteLLM metric names drift between releases. These match recent `main-stable`; if a panel
// is empty, check the live names at `kubectl port-forward svc/litellm 4000:4000` → :4000/metrics
// and adjust here (single source — only this file references them).
local ee = import '../lib/ee.libsonnet';
local p = ee.dsProm;

ee.dashboard('Eval Engine — Model Gateway', 'ee-gateway', [
  ee.row('Now'),
  // At-a-glance KPI strip (mixed units → four compact stats rather than one block).
  ee.stat('Request rate', p, [ee.prom('sum(rate(litellm_proxy_total_requests_metric_total[$__rate_interval]))')], 'reqps'),
  ee.stat('Failed rate', p, [ee.prom('sum(rate(litellm_proxy_failed_requests_metric_total[$__rate_interval]))')], 'reqps'),
  ee.stat('Latency p95', p, [ee.prom('histogram_quantile(0.95, sum(rate(litellm_request_total_latency_metric_bucket[$__rate_interval])) by (le))')], 's'),
  ee.stat('Spend (tally)', p, [ee.prom('sum(litellm_spend_metric_total)')], 'currencyUSD'),

  ee.row('Throughput & errors'),
  ee.timeseries('Request rate by model', p, [ee.prom(
    'sum(rate(litellm_proxy_total_requests_metric_total[$__rate_interval])) by (model)', '{{model}}',
  )], 'reqps'),
  ee.timeseries('Failed request rate', p, [ee.prom(
    'sum(rate(litellm_proxy_failed_requests_metric_total[$__rate_interval])) by (model)', '{{model}}',
  )], 'reqps'),

  ee.row('Latency'),
  ee.timeseries('Gateway latency p50/p95/p99', p, [
    ee.prom('histogram_quantile(0.50, sum(rate(litellm_request_total_latency_metric_bucket[$__rate_interval])) by (le))', 'p50', 'A'),
    ee.prom('histogram_quantile(0.95, sum(rate(litellm_request_total_latency_metric_bucket[$__rate_interval])) by (le))', 'p95', 'B'),
    ee.prom('histogram_quantile(0.99, sum(rate(litellm_request_total_latency_metric_bucket[$__rate_interval])) by (le))', 'p99', 'C'),
  ], 's'),

  ee.row('Tokens & spend'),
  ee.timeseries('Token rate', p, [ee.prom(
    'sum(rate(litellm_total_tokens_total[$__rate_interval])) by (model)', '{{model}}',
  )]),
  ee.timeseries('Spend (gateway tally) by model', p, [ee.prom(
    'sum(litellm_spend_metric_total) by (model)', '{{model}}',
  )], 'currencyUSD'),
])
