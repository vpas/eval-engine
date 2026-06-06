// Shared helpers for all Eval Engine dashboards.
//
// Everything fragile about the grafonnet / datasource-plugin API surface is funnelled through
// here, so if a panel or target schema needs a tweak it is a one-line fix in this file rather
// than across five dashboards. Dashboards import this as `ee` and only ever call these helpers.
local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  g:: g,

  // ---- datasource references -------------------------------------------------------------
  // The three provisioned datasources are pinned by stable uid (see deploy/k8s/95-grafana.yaml).
  // We reference them via dashboard *variables* so a single dashboard can be repointed (e.g. a
  // staging ClickHouse) without editing every panel.
  dsCH:: { type: 'grafana-clickhouse-datasource', uid: '${ds_clickhouse}' },
  dsPG:: { type: 'grafana-postgresql-datasource', uid: '${ds_postgres}' },
  dsProm:: { type: 'prometheus', uid: '${ds_prometheus}' },

  // ---- query targets ---------------------------------------------------------------------
  // ClickHouse plugin (v4): editorType=sql + rawSql; format 0=table, 1=time-series, 2=logs.
  chTS(sql, refId='A'):: {
    datasource: $.dsCH,
    refId: refId,
    editorType: 'sql',
    rawSql: sql,
    format: 1,
  },
  chTable(sql, refId='A'):: {
    datasource: $.dsCH,
    refId: refId,
    editorType: 'sql',
    rawSql: sql,
    format: 0,
  },
  // Postgres (built-in): format 'time_series' expects a `time` column + numeric metrics;
  // 'table' returns rows as-is.
  pgTS(sql, refId='A'):: {
    datasource: $.dsPG,
    refId: refId,
    rawSql: sql,
    format: 'time_series',
  },
  pgTable(sql, refId='A'):: {
    datasource: $.dsPG,
    refId: refId,
    rawSql: sql,
    format: 'table',
  },
  prom(expr, legend='', refId='A'):: {
    datasource: $.dsProm,
    refId: refId,
    expr: expr,
    legendFormat: legend,
    range: true,
  },

  // ---- panel constructors ----------------------------------------------------------------
  // Each takes a datasource object + targets and returns a grafonnet panel. Datasource is set by
  // direct object-merge (panels are plain objects) so we don't depend on withDatasource's arity.
  timeseries(title, ds, targets, unit='short')::
    g.panel.timeSeries.new(title)
    + g.panel.timeSeries.queryOptions.withTargets(targets)
    + g.panel.timeSeries.standardOptions.withUnit(unit)
    + g.panel.timeSeries.options.legend.withDisplayMode('table')
    + g.panel.timeSeries.options.legend.withPlacement('bottom')
    + { datasource: ds },

  stat(title, ds, targets, unit='short')::
    g.panel.stat.new(title)
    + g.panel.stat.queryOptions.withTargets(targets)
    + g.panel.stat.standardOptions.withUnit(unit)
    + g.panel.stat.options.withColorMode('value')
    + { datasource: ds },

  gauge(title, ds, targets, unit='short', max=null)::
    g.panel.gauge.new(title)
    + g.panel.gauge.queryOptions.withTargets(targets)
    + g.panel.gauge.standardOptions.withUnit(unit)
    + (if max != null then g.panel.gauge.standardOptions.withMax(max) else {})
    + { datasource: ds },

  barchart(title, ds, targets, unit='short')::
    g.panel.barChart.new(title)
    + g.panel.barChart.queryOptions.withTargets(targets)
    + g.panel.barChart.standardOptions.withUnit(unit)
    + { datasource: ds },

  table(title, ds, targets, unit=null)::
    g.panel.table.new(title)
    + g.panel.table.queryOptions.withTargets(targets)
    + (if unit != null then g.panel.table.standardOptions.withUnit(unit) else {})
    + { datasource: ds },

  row(title)::
    g.panel.row.new(title),

  // ---- dashboard assembly ----------------------------------------------------------------
  // makeGrid auto-lays-out panels (no hand-maintained gridPos). Rows force a line break.
  dashboard(title, uid, panels, vars=[], refresh='30s', from='now-6h')::
    g.dashboard.new(title)
    + g.dashboard.withUid(uid)
    + g.dashboard.withTags(['eval-engine'])
    + g.dashboard.withRefresh(refresh)
    + g.dashboard.withTimezone('browser')
    + g.dashboard.time.withFrom(from)
    + g.dashboard.time.withTo('now')
    + g.dashboard.withVariables($.commonVars + vars)
    + g.dashboard.withPanels(g.util.grid.makeGrid(panels, panelWidth=12, panelHeight=8)),

  // ---- variables -------------------------------------------------------------------------
  // Datasource picker variables (hidden — they default to the provisioned uids). Putting the
  // datasource behind a variable is what lets `${ds_clickhouse}` resolve in every target.
  dsVar(name, pluginId)::
    g.dashboard.variable.datasource.new(name, pluginId)
    + g.dashboard.variable.datasource.generalOptions.showOnDashboard.withNothing(),

  commonVars:: [
    $.dsVar('ds_clickhouse', 'grafana-clickhouse-datasource'),
    $.dsVar('ds_postgres', 'grafana-postgresql-datasource'),
    $.dsVar('ds_prometheus', 'prometheus'),
  ],

  // A ClickHouse-backed multi-value query variable (with an "All" entry) for slicing.
  chQueryVar(name, sql, includeAll=true)::
    g.dashboard.variable.query.new(name, sql)
    + g.dashboard.variable.query.withDatasource('grafana-clickhouse-datasource', '${ds_clickhouse}')
    + g.dashboard.variable.query.selectionOptions.withIncludeAll(includeAll)
    + g.dashboard.variable.query.selectionOptions.withMulti(true),
}
