// Eval Quality — "is the platform producing good evals?"  Source: ClickHouse sample_results.
local ee = import '../lib/ee.libsonnet';
local ds = ee.dsCH;

// Slicing variables (ClickHouse-backed, multi-select + All).
local vEval = ee.chQueryVar('eval', 'SELECT DISTINCT toString(eval_id) FROM sample_results ORDER BY 1');
local vModel = ee.chQueryVar('model', 'SELECT DISTINCT model_id FROM sample_results ORDER BY 1');

// Common WHERE: time window + the two slice vars. `${var:singlequote}` expands a multi-select
// into a quoted, comma-separated IN list (All → every option).
local where = |||
  WHERE $__timeFilter(finished_at)
    AND toString(eval_id) IN (${eval:singlequote})
    AND model_id IN (${model:singlequote})
|||;

ee.dashboard('Eval Engine — Quality', 'ee-eval-quality', [
  ee.row('Headline (selected window)'),
  ee.stat('Pass rate', ds, [ee.chTable('SELECT avg(passed) ' + where, 'A')], 'percentunit'),
  ee.stat('Samples evaluated', ds, [ee.chTable('SELECT count() ' + where, 'A')]),
  ee.stat('Mean primary score', ds, [ee.chTable('SELECT avg(primary_score) ' + where, 'A')]),
  ee.stat('Error rate', ds, [ee.chTable("SELECT countIf(error_type != '') / count() " + where, 'A')], 'percentunit'),

  ee.row('Quality over time'),
  ee.timeseries('Pass rate by model', ds, [ee.chTS(
    'SELECT $__timeInterval(finished_at) AS time, model_id, avg(passed) AS pass_rate ' +
    where + ' GROUP BY time, model_id ORDER BY time', 'A',
  )], 'percentunit'),
  ee.timeseries('Throughput (samples/min)', ds, [ee.chTS(
    'SELECT $__timeInterval(finished_at) AS time, count() AS samples ' +
    where + ' GROUP BY time ORDER BY time', 'A',
  )]),

  ee.row('Slices'),
  ee.barchart('Pass rate by category', ds, [ee.chTable(
    "SELECT category, avg(passed) AS pass_rate FROM sample_results " + where +
    " AND category != '' GROUP BY category ORDER BY pass_rate", 'A',
  )], 'percentunit'),
  ee.timeseries('Error rate by error_type', ds, [ee.chTS(
    "SELECT $__timeInterval(finished_at) AS time, error_type, count() AS n FROM sample_results " +
    where + " AND error_type != '' GROUP BY time, error_type ORDER BY time", 'A',
  )]),

  ee.row('Leaderboard'),
  // Table (16) + attempt-distribution bar (8) share one 24-col line.
  ee.size(ee.table('Model × eval leaderboard', ds, [ee.chTable(
    |||
      SELECT model_id, toString(eval_id) AS eval, count() AS n,
             round(avg(passed), 4) AS pass_rate,
             round(avg(primary_score), 4) AS mean_score,
             round(sum(cost_usd), 4) AS cost_usd
      FROM sample_results
    ||| + where + ' GROUP BY model_id, eval ORDER BY pass_rate DESC', 'A',
  )]), 16, 8),
  ee.size(ee.barchart('Attempt distribution (retries)', ds, [ee.chTable(
    'SELECT toString(attempt) AS attempt, count() AS n FROM sample_results ' +
    where + ' GROUP BY attempt ORDER BY attempt', 'A',
  )]), 8, 8),
], vars=[vEval, vModel])
