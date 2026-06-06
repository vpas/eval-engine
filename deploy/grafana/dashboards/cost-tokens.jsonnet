// Cost & Tokens — the spend guardrail. Source: ClickHouse sample_results (history) + Postgres
// runs (live budget burn on in-flight runs).
local ee = import '../lib/ee.libsonnet';
local ch = ee.dsCH;
local pg = ee.dsPG;

local where = 'WHERE $__timeFilter(finished_at)';

ee.dashboard('Eval Engine — Cost & Tokens', 'ee-cost-tokens', [
  ee.row('Spend (selected window)'),
  ee.stat('Total cost', ch, [ee.chTable('SELECT sum(cost_usd) ' + where, 'A')], 'currencyUSD'),
  ee.stat('Cost / 1k tokens', ch, [ee.chTable(
    'SELECT sum(cost_usd) / (sum(tokens_in + tokens_out) / 1000.0) ' + where, 'A',
  )], 'currencyUSD'),
  ee.stat('Tokens (in)', ch, [ee.chTable('SELECT sum(tokens_in) ' + where, 'A')]),
  ee.stat('Tokens (out)', ch, [ee.chTable('SELECT sum(tokens_out) ' + where, 'A')]),

  ee.row('Cost over time'),
  ee.timeseries('Cost rate by provider', ch, [ee.chTS(
    'SELECT $__timeInterval(finished_at) AS time, provider, sum(cost_usd) AS cost ' +
    where + ' GROUP BY time, provider ORDER BY time', 'A',
  )], 'currencyUSD'),
  ee.timeseries('Cumulative spend', ch, [ee.chTS(
    |||
      SELECT time, sum(cost) OVER (ORDER BY time) AS cumulative FROM (
        SELECT $__timeInterval(finished_at) AS time, sum(cost_usd) AS cost
        FROM sample_results
    ||| + where + ' GROUP BY time ORDER BY time)', 'A',
  )], 'currencyUSD'),

  ee.row('Tokens'),
  ee.timeseries('Token throughput (in/out per min)', ch, [
    ee.chTS('SELECT $__timeInterval(finished_at) AS time, sum(tokens_in) AS tokens_in ' + where + ' GROUP BY time ORDER BY time', 'A'),
    ee.chTS('SELECT $__timeInterval(finished_at) AS time, sum(tokens_out) AS tokens_out ' + where + ' GROUP BY time ORDER BY time', 'B'),
  ]),
  ee.table('Cost by model', ch, [ee.chTable(
    |||
      SELECT model_id, count() AS n, round(sum(cost_usd), 4) AS cost_usd,
             sum(tokens_in) AS tok_in, sum(tokens_out) AS tok_out,
             round(sum(cost_usd) / count(), 6) AS cost_per_sample
      FROM sample_results
    ||| + where + ' GROUP BY model_id ORDER BY cost_usd DESC', 'A',
  )]),

  ee.row('Live budget burn (in-flight runs)'),
  // Live cost is published on the Postgres runs row by the orchestrator tick; budget cap lives in
  // the RunSpec. Burn = spent / max_usd. Runs without a budget show null (no cap).
  ee.table('Active-run budget burn', pg, [ee.pgTable(
    |||
      SELECT r.id::text AS run_id, r.status,
             r.total_cost_usd AS spent_usd,
             (rs.budget->>'max_usd')::numeric AS budget_usd,
             CASE WHEN (rs.budget->>'max_usd') IS NOT NULL AND (rs.budget->>'max_usd')::numeric > 0
                  THEN round(r.total_cost_usd / (rs.budget->>'max_usd')::numeric, 4) END AS burn_frac
      FROM runs r JOIN run_specs rs ON rs.id = r.run_spec_id
      WHERE r.status IN ('queued','expanding','running','finalizing')
      ORDER BY burn_frac DESC NULLS LAST
    |||, 'A',
  )]),
])
