"""Operational status aggregation for the ops dashboard (a single ``GET /ops/status`` snapshot).

Probes every subsystem concurrently with a per-probe timeout, returning a uniform component shape::

    {name, status, detail, metrics, logs_url, last_seen}

Two signal tiers, both degrade gracefully (an unreachable backend or an unconfigured cloud → a
``down``/``unknown`` component, never an exception out of ``snapshot()``):

  * **Portable application health** — PG / ClickHouse / GCS probes the control plane can already do,
    plus PG-backed **heartbeats** the orchestrator + workers write (so the leader-elected singleton
    and the KEDA-scaled workers become observable with no Kubernetes coupling — works on EKS/AKS/local).
  * **Cloud infra truth** — the in-cluster **Kubernetes API** (pod phase / restarts / ready replicas,
    KEDA desired-vs-current) + **GCP Log Explorer** deep links. Active only in-cluster with a
    configured project; absent in dev.

Nothing here ships log *contents*: we hand the operator a pre-filtered Log Explorer URL (no extra
storage, quota, or PII surface) and let Cloud Logging do the tailing.
"""
from __future__ import annotations

import concurrent.futures
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .db import analytics, control

# --- cluster / cloud config (env-driven; absent ⇒ the cloud-specific bits no-op in dev) -----------
GCP_PROJECT = os.environ.get("EVAL_ENGINE_GCP_PROJECT")
GKE_CLUSTER = os.environ.get("EVAL_ENGINE_GKE_CLUSTER", "eval-engine")
GKE_ZONE = os.environ.get("EVAL_ENGINE_GKE_ZONE", "")
NAMESPACE = os.environ.get("EVAL_ENGINE_K8S_NAMESPACE", "eval-engine")
SANDBOX_NS = os.environ.get("INSPECT_K8S_DEFAULT_NAMESPACE", "eval-sandbox")
ORCH_TICK = float(os.environ.get("EVAL_ENGINE_ORCH_TICK", "2.0"))
WORKER_POLL = float(os.environ.get("EVAL_ENGINE_WORKER_POLL", "1.0"))
GLOBAL_MAX_RUNNING = int(os.environ.get("EVAL_ENGINE_GLOBAL_MAX_RUNNING", "50"))
INTERACTIVE_RESERVE = int(os.environ.get("EVAL_ENGINE_INTERACTIVE_RESERVE", "12"))

# A live heartbeat is fresher than a few of its own loops; past this it's stale (crashed/scaled-down).
ORCH_STALE_S = max(3 * ORCH_TICK, 10.0)
WORKER_STALE_S = float(os.environ.get("EVAL_ENGINE_WORKER_STALE_SECONDS", "30"))

# component name → the k8s `app` label (for log links + merging in the Kubernetes probe's pod truth).
COMPONENT_APP = {
    "api": "eval-engine-api", "orchestrator": "eval-engine-orch", "workers": "eval-engine-worker",
    "litellm": "litellm", "clickhouse": "clickhouse", "redis": "redis", "inspect_view": "inspect-view",
}
# postgres = Neon (external, no GKE logs); gcs = managed → no container logs for those two.
CRITICAL = {"postgres", "clickhouse"}


# --- GCP Log Explorer deep links -----------------------------------------------------------------

def log_url(*, container: str | None = None, pod: str | None = None, app: str | None = None,
            run_id: str | None = None, namespace: str | None = None,
            severity: str | None = None, minutes: int = 60) -> str | None:
    """Build a Cloud Logging *Log Explorer* deep link pre-filtered to a component/pod/run, or ``None``
    when no GCP project is configured (dev — the UI then hides the button). The query is the Logging
    query language; the console reads it from the ``;query=`` path segment (URL-encoded), the time
    window from ``;duration=`` and the project from ``?project=``."""
    if not GCP_PROJECT:
        return None
    ns = namespace or NAMESPACE
    lines = ['resource.type="k8s_container"',
             f'resource.labels.cluster_name="{GKE_CLUSTER}"',
             f'resource.labels.namespace_name="{ns}"']
    if container:
        lines.append(f'resource.labels.container_name="{container}"')
    if app:
        lines.append(f'labels."k8s-pod/app"="{app}"')
    if pod:
        lines.append(f'resource.labels.pod_name="{pod}"')
    if run_id:
        lines.append(f'"{run_id}"')          # free-text match in the log payload
    if severity:
        lines.append(f"severity>={severity}")
    q = urllib.parse.quote("\n".join(lines), safe="")
    return (f"https://console.cloud.google.com/logs/query;query={q};duration=PT{int(minutes)}M"
            f"?project={GCP_PROJECT}")


# --- probe plumbing ------------------------------------------------------------------------------

def _comp(name: str, status: str, detail: str = "", metrics: dict | None = None,
          logs_url: str | object = "__auto__", last_seen: str | None = None) -> dict:
    if logs_url == "__auto__":
        logs_url = log_url(app=COMPONENT_APP[name]) if name in COMPONENT_APP else None
    return {"name": name, "status": status, "detail": detail, "metrics": metrics or {},
            "logs_url": logs_url, "last_seen": last_seen}


def _http_status(url: str, timeout: float = 1.5) -> int:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code


# --- individual probes ---------------------------------------------------------------------------

def probe_postgres() -> dict:
    t = time.time()
    counts = control.global_ledger_counts()
    conns = control.pg_connections()
    runs = control.run_status_counts()
    return _comp("postgres", "ok", "Neon serverless · metadata + ephemeral ledger",
                 {"ledger_rows": sum(counts.values()), "connections": conns,
                  "running_runs": runs.get("running", 0), "queued_runs": runs.get("queued", 0),
                  "latency_ms": round((time.time() - t) * 1000)},
                 logs_url=None)


def probe_clickhouse() -> dict:
    t = time.time()
    h = analytics.health()
    metrics = {"rows": h["rows"], "latency_ms": round((time.time() - t) * 1000)}
    status, detail = "ok", "ReplacingMergeTree · 12-mo TTL"
    rep = h.get("replicas")
    if rep:
        metrics["replicas"] = f"{rep['active']}/{rep['total']}"
        metrics["repl_lag_s"] = rep["delay_s"]
        detail = "ReplicatedReplacingMergeTree · Keeper quorum"
        if rep["active"] < rep["total"]:
            status = "degraded"
            detail = f"{rep['total'] - rep['active']} replica(s) down"
    return _comp("clickhouse", status, detail, metrics)


def probe_redis() -> dict:
    host = os.environ.get("EVAL_ENGINE_REDIS_HOST", "redis")
    port = int(os.environ.get("EVAL_ENGINE_REDIS_PORT", "6379"))
    try:
        import redis  # optional dep (ops extra)
    except ImportError:
        return _comp("redis", "unknown", "redis client not installed")
    r = redis.Redis(host=host, port=port, socket_timeout=1.5, socket_connect_timeout=1.5)
    r.ping()
    info = r.info("replication")
    role, slaves = info.get("role", "?"), int(info.get("connected_slaves", 0))
    status = "ok" if role == "master" or slaves >= 0 else "degraded"
    return _comp("redis", status, f"{role} · {slaves} replicas · Sentinel HA",
                 {"role": role, "connected_replicas": slaves})


def probe_litellm() -> dict:
    base = os.environ.get("EVAL_ENGINE_LITELLM_URL")
    if not base:
        ob = os.environ.get("OPENAI_BASE_URL", "http://litellm:4000/v1")
        base = ob[:-3] if ob.endswith("/v1") else ob
    code = _http_status(base.rstrip("/") + "/health/liveliness")
    status = "ok" if code == 200 else "degraded"
    return _comp("litellm", status, "model gateway · global rate-limit (Redis)",
                 {"http": code})


def probe_inspect_view() -> dict:
    url = os.environ.get("EVAL_ENGINE_VIEWER_URL", "http://inspect-view:7575") + "/"
    code = _http_status(url)
    return _comp("inspect_view", "ok" if code < 500 else "degraded",
                 "embedded Inspect trace viewer", {"http": code})


def probe_gcs() -> dict:
    bucket = os.environ.get("EVAL_ENGINE_GCS_BUCKET")
    if not bucket:
        return _comp("gcs", "unknown", "no bucket configured (local fs)", logs_url=None)
    from . import storage
    uri = f"gs://{bucket}/ops/healthz"
    t = time.time()
    storage.write_bytes(uri, b"ok")
    ok = storage.read_bytes(uri) == b"ok"
    return _comp("gcs", "ok" if ok else "degraded", f"object store · {bucket}",
                 {"latency_ms": round((time.time() - t) * 1000)}, logs_url=None)


def probe_orchestrator(hbs: list[dict]) -> dict:
    rows = [h for h in hbs if h["component"] == "orchestrator"]
    if not rows:
        return _comp("orchestrator", "unknown", "no heartbeat (not yet started?)")
    leader = min((h for h in rows if h["detail"].get("leader")), key=lambda h: h["age_s"], default=None)
    fresh = leader or min(rows, key=lambda h: h["age_s"])
    age = fresh["age_s"]
    d = fresh["detail"]
    status = "ok" if age < ORCH_STALE_S else "down"
    detail = (f"leader {fresh['instance']}" if leader else "no live leader (standby only)")
    if not leader:
        status = "degraded"
    return _comp("orchestrator", status, detail,
                 {"tick_age_s": round(age, 1), "running_runs": d.get("running_runs"),
                  "admitted": d.get("admitted"), "standbys": max(0, len(rows) - 1)},
                 last_seen=f"{round(age, 1)}s ago")


def probe_workers(hbs: list[dict], queues: dict) -> dict:
    rows = [h for h in hbs if h["component"] == "worker"]
    live = [h for h in rows if h["age_s"] < WORKER_STALE_S]
    n = len(live)
    claims = sum(int(h["detail"].get("claimed_this_loop", 0) or 0) for h in live)
    queued = (queues.get("ledger") or {}).get("queued", 0)
    if n == 0:
        # No live workers is fine when there's nothing to do (KEDA scaled to 0); a problem otherwise.
        status = "degraded" if queued > 0 else "idle"
        detail = "0 live · work queued (scaling up?)" if queued > 0 else "0 live · idle (scaled to 0)"
    else:
        status = "ok"
        detail = f"{n} live · {claims} in-flight claims"
    return _comp("workers", status, detail, {"live": n, "claims": claims})


def probe_api() -> dict:
    # We *are* the API serving this; report self + the pod for log drill-in.
    return _comp("api", "ok", "FastAPI control plane",
                 {"pod": os.environ.get("HOSTNAME", "local")})


def probe_kubernetes() -> dict | None:
    """Cloud infra truth: per-workload ready/desired + per-pod phase/restarts/node, and KEDA's
    desired-vs-current. ``None`` (silently omitted) when the k8s client or in-cluster config is
    absent — i.e. local dev — so the rest of the snapshot is unaffected."""
    try:
        from kubernetes import client, config
        config.load_incluster_config()
    except Exception:  # noqa: BLE001 — not in-cluster / client missing
        return None
    apps, core = client.AppsV1Api(), client.CoreV1Api()
    workloads = []
    for ns in (NAMESPACE, SANDBOX_NS):
        try:
            deps = apps.list_namespaced_deployment(ns, timeout_seconds=2).items
        except Exception:  # noqa: BLE001
            continue
        for d in deps:
            app = (d.spec.selector.match_labels or {}).get("app", d.metadata.name)
            pods = []
            try:
                plist = core.list_namespaced_pod(ns, label_selector=f"app={app}", timeout_seconds=2).items
            except Exception:  # noqa: BLE001
                plist = []
            for p in plist:
                restarts = sum((cs.restart_count or 0) for cs in (p.status.container_statuses or []))
                ready = all(cs.ready for cs in (p.status.container_statuses or [])) and bool(p.status.container_statuses)
                pods.append({"name": p.metadata.name, "phase": p.status.phase, "ready": ready,
                             "restarts": restarts, "node": p.spec.node_name,
                             "logs_url": log_url(pod=p.metadata.name, namespace=ns)})
            workloads.append({"app": app, "namespace": ns,
                              "ready": d.status.ready_replicas or 0,
                              "desired": d.spec.replicas or 0, "pods": pods})
    keda = []
    try:
        co = client.CustomObjectsApi()
        sos = co.list_namespaced_custom_object("keda.sh", "v1alpha1", NAMESPACE, "scaledobjects")
        for so in sos.get("items", []):
            st = so.get("status", {})
            keda.append({"name": so["metadata"]["name"],
                         "target": st.get("scaleTargetGVKR", {}).get("kind"),
                         "min": (so["spec"].get("minReplicaCount")),
                         "max": (so["spec"].get("maxReplicaCount")),
                         "current": st.get("originalReplicaCount")})
    except Exception:  # noqa: BLE001
        pass
    return {"workloads": workloads, "keda": keda}


# --- the snapshot --------------------------------------------------------------------------------

_thr = {"ts": None, "done": None}  # tiny rolling-throughput cache (single api replica; approximate)


def _throughput(done_total: int) -> float | None:
    now = time.time()
    rate = None
    if _thr["ts"] is not None and now > _thr["ts"]:
        rate = max(0.0, (done_total - _thr["done"]) / (now - _thr["ts"]))
    _thr["ts"], _thr["done"] = now, done_total
    return rate


def _queue_rollup() -> dict:
    ledger = control.global_ledger_counts()
    lanes = control.lane_running_counts()
    runs = control.run_status_counts()
    done_total = ledger.get("done", 0)
    return {
        "ledger": ledger,
        "lanes": lanes,
        "runs": runs,
        "samples_per_s": _throughput(done_total),
        "admission": {"global_max": GLOBAL_MAX_RUNNING, "interactive_reserve": INTERACTIVE_RESERVE,
                      "running": (runs.get("running", 0))},
    }


def snapshot() -> dict:
    """Full ops snapshot: cluster meta + per-component health (with log links) + live queue rollup +
    Kubernetes workloads + active runs + recent failures. Each probe is isolated, so one slow/broken
    subsystem can't take down the others (or the endpoint)."""
    hbs = control.list_heartbeats()
    queues = _queue_rollup()

    # Independent, possibly-slow probes run concurrently and bounded; a failure → a `down` component.
    net_probes = {"postgres": probe_postgres, "clickhouse": probe_clickhouse, "redis": probe_redis,
                  "litellm": probe_litellm, "inspect_view": probe_inspect_view, "gcs": probe_gcs,
                  "kubernetes": probe_kubernetes, "api": probe_api}
    results: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(net_probes)) as ex:
        futs = {name: ex.submit(fn) for name, fn in net_probes.items()}
        for name, fut in futs.items():
            try:
                results[name] = fut.result(timeout=3.0)
            except Exception as e:  # noqa: BLE001
                results[name] = _comp(name, "down", str(e)[:160])

    k8s = results.pop("kubernetes", None)
    # heartbeat-derived components (cheap PG reads already in hand)
    results["orchestrator"] = probe_orchestrator(hbs)
    results["workers"] = probe_workers(hbs, queues)

    # Enrich app-backed components with the Kubernetes pod truth (ready/desired + restarts).
    workloads = (k8s or {}).get("workloads", []) if isinstance(k8s, dict) else []
    by_app = {w["app"]: w for w in workloads}
    for name, comp in results.items():
        w = by_app.get(COMPONENT_APP.get(name, ""))
        if w:
            comp["metrics"]["pods"] = f"{w['ready']}/{w['desired']}"
            comp["metrics"]["restarts"] = sum(p["restarts"] for p in w["pods"])
            if w["desired"] and not w["ready"] and comp["status"] == "ok":
                comp["status"] = "degraded"

    order = ["api", "orchestrator", "workers", "postgres", "clickhouse", "redis", "litellm",
             "gcs", "inspect_view"]
    components = [results[n] for n in order if n in results]

    crit_down = any(c["status"] == "down" for c in components if c["name"] in CRITICAL)
    any_bad = any(c["status"] in ("down", "degraded") for c in components)
    overall = "down" if crit_down else ("degraded" if any_bad else "ok")

    return {
        "cluster": {"project": GCP_PROJECT, "cluster": GKE_CLUSTER, "zone": GKE_ZONE,
                    "namespace": NAMESPACE},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "components": components,
        "workloads": workloads,
        "keda": (k8s or {}).get("keda", []) if isinstance(k8s, dict) else [],
        "queues": queues,
        "active_runs": [
            {**r, "logs_url": log_url(container="worker", run_id=r["id"])}
            for r in control.active_runs_detail()
        ],
        "failures": [
            {**f, "logs_url": log_url(container="worker", run_id=f["run_id"], severity="ERROR")}
            for f in control.recent_failures(15)
        ],
        "audit": control.list_audit(15),
    }
