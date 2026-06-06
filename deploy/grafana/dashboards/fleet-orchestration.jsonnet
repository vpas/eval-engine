// Fleet & Orchestration — the distributed engine's live health. Source: Postgres control plane
// (runs + sample_tasks ledger + heartbeats + audit_log). This is the "now", not the warehouse.
local ee = import '../lib/ee.libsonnet';
local pg = ee.dsPG;

ee.dashboard('Eval Engine — Fleet & Orchestration', 'ee-fleet', [
  ee.row('Live state'),
  ee.stat('Running runs', pg, [ee.pgTable("SELECT count(*) FROM runs WHERE status = 'running'", 'A')]),
  ee.stat('Queued runs', pg, [ee.pgTable("SELECT count(*) FROM runs WHERE status IN ('queued','expanding')", 'A')]),
  ee.stat('In-flight sample tasks', pg, [ee.pgTable("SELECT count(*) FROM sample_tasks WHERE status IN ('queued','running')", 'A')]),
  ee.stat('Active workers', pg, [ee.pgTable(
    "SELECT count(DISTINCT instance) FROM heartbeats WHERE component = 'worker' AND ts > now() - interval '2 minutes'", 'A',
  )]),

  ee.row('Runs & ledger'),
  ee.barchart('Runs by status', pg, [ee.pgTable('SELECT status, count(*) AS n FROM runs GROUP BY status ORDER BY n DESC', 'A')]),
  ee.barchart('Ledger tasks by status (active runs)', pg, [ee.pgTable('SELECT status, count(*) AS n FROM sample_tasks GROUP BY status ORDER BY n DESC', 'A')]),

  ee.row('Orchestration health'),
  // The orchestrator upserts {leader, running_runs, tick_ms} into heartbeats every tick; workers
  // upsert {claimed_this_loop}. Those rows are our orchestration time-series.
  ee.timeseries('Orchestrator tick latency', pg, [ee.pgTS(
    |||
      SELECT $__timeGroupAlias(ts, $__interval), instance,
             avg((detail->>'tick_ms')::float) AS tick_ms
      FROM heartbeats
      WHERE component = 'orchestrator' AND $__timeFilter(ts)
      GROUP BY 1, instance ORDER BY 1
    |||, 'A',
  )], 'ms'),
  ee.timeseries('Worker claims / loop', pg, [ee.pgTS(
    |||
      SELECT $__timeGroupAlias(ts, $__interval),
             sum((detail->>'claimed_this_loop')::int) AS claimed
      FROM heartbeats
      WHERE component = 'worker' AND $__timeFilter(ts)
      GROUP BY 1 ORDER BY 1
    |||, 'A',
  )]),

  ee.row('Reliability'),
  // Lease expiry = a worker died mid-sample; the row is reclaimable. High max(attempts) = a poison
  // sample being retried. Both are early-warning signals the live snapshot dashboard can't trend.
  ee.stat('Expired leases (reclaimable)', pg, [ee.pgTable(
    "SELECT count(*) FROM sample_tasks WHERE status = 'running' AND lease_expires_at < now()", 'A',
  )]),
  ee.stat('Max task attempts', pg, [ee.pgTable('SELECT coalesce(max(attempts), 0) FROM sample_tasks', 'A')]),
  ee.timeseries('Runs launched / hour', pg, [ee.pgTS(
    |||
      SELECT $__timeGroupAlias(queued_at, '1h'), count(*) AS launched
      FROM runs WHERE $__timeFilter(queued_at) GROUP BY 1 ORDER BY 1
    |||, 'A',
  )]),
  ee.timeseries('Audit activity / hour', pg, [ee.pgTS(
    |||
      SELECT $__timeGroupAlias(ts, '1h'), action, count(*) AS n
      FROM audit_log WHERE $__timeFilter(ts) GROUP BY 1, action ORDER BY 1
    |||, 'A',
  )]),

  ee.row('Latency (completed runs in window)'),
  ee.table('Recent run timings', pg, [ee.pgTable(
    |||
      SELECT id::text AS run_id, status,
             round(extract(epoch FROM (started_at - queued_at)))   AS queue_wait_s,
             round(extract(epoch FROM (finished_at - started_at)))  AS duration_s,
             done_samples, failed_samples, total_samples, round(total_cost_usd, 4) AS cost_usd
      FROM runs
      WHERE finished_at IS NOT NULL AND $__timeFilter(finished_at)
      ORDER BY finished_at DESC LIMIT 50
    |||, 'A',
  )]),
])
