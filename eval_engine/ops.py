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
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .config import GLOBAL_MAX_RUNNING, INTERACTIVE_RESERVE, ORCH_TICK_SECONDS
from .db import analytics, control

# --- cluster / cloud config (env-driven; absent ⇒ the cloud-specific bits no-op in dev) -----------
GCP_PROJECT = os.environ.get("EVAL_ENGINE_GCP_PROJECT")
GKE_CLUSTER = os.environ.get("EVAL_ENGINE_GKE_CLUSTER", "eval-engine")
GKE_ZONE = os.environ.get("EVAL_ENGINE_GKE_ZONE", "")
NAMESPACE = os.environ.get("EVAL_ENGINE_K8S_NAMESPACE", "eval-engine")
SANDBOX_NS = os.environ.get("INSPECT_K8S_DEFAULT_NAMESPACE", "eval-sandbox")
# GLOBAL_MAX_RUNNING / INTERACTIVE_RESERVE / ORCH_TICK_SECONDS are imported from config (one
# definition, shared with the orchestrator that enforces them — so the dashboard never shows a
# stale default).

# A live heartbeat is fresher than a few of its own loops; past this it's stale (crashed/scaled-down).
ORCH_STALE_S = max(3 * ORCH_TICK_SECONDS, 10.0)
WORKER_STALE_S = float(os.environ.get("EVAL_ENGINE_WORKER_STALE_SECONDS", "30"))

# Scale-from-0 is normal, not a fault: KEDA polls (~30s) then a pod must schedule + pull the image
# before it can heartbeat. Queued work with no ready worker reads as `scaling` (informational) until
# it has waited past this grace window with nothing coming up — only then is it a genuine stall.
WORKER_SCALEUP_GRACE_S = float(os.environ.get("EVAL_ENGINE_WORKER_SCALEUP_GRACE_SECONDS", "180"))
WORKER_CRASH_RESTARTS = int(os.environ.get("EVAL_ENGINE_WORKER_CRASH_RESTARTS", "3"))
# Container waiting reasons that mean "broken", not "still coming up".
WORKER_CRASH_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull",
                        "InvalidImageName", "CreateContainerError", "CreateContainerConfigError",
                        "RunContainerError"}


def _fmt_dur(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"

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
    r.ping()  # reachability — a failure raises and snapshot() reports the component `down`
    info = r.info("replication")
    role, slaves = info.get("role", "?"), int(info.get("connected_slaves", 0))
    # Status is reachability-only, by design. The `redis` Service selects all three StatefulSet pods
    # (there is no master-only Service), so a single point-in-time probe round-robins onto the master
    # OR a replica at random — `role`/`connected_slaves` describe whichever node answered, not the
    # cluster, and so can't honestly drive a `degraded` verdict (it would just flap with node choice).
    # Authoritative replication health needs a Sentinel query (SENTINEL master/replicas/ckquorum on
    # :26379); deferred — see docs/CODE_REVIEW.md. Until then we surface the node's role + replica
    # count as informational only.
    return _comp("redis", "ok", f"reachable · this node: {role} · {slaves} replica(s) · Sentinel HA",
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


def probe_workers(hbs: list[dict], queues: dict, pods: list[dict] | None = None,
                  queued_age_s: float | None = None) -> dict:
    """Worker liveness, distinguishing a *normal scale-from-0* from a *real* degradation.

    The hard case is "queued work, no heartbeating worker": that's exactly what both a healthy
    KEDA spin-up AND a stuck/broken worker pool look like from Postgres alone. We disambiguate with
    the Kubernetes pod truth + how long work has actually been waiting (``queued_age_s``):

      * a ready pod, just no fresh heartbeat → **ok** (busy, blocked mid-batch in a long model call);
      * pods being created (ContainerCreating / Pending) → **scaling** (KEDA activated, coming up);
      * no pods yet but the queue is younger than the grace window → **scaling** (KEDA activating);
      * pods crash-looping / image-pull-failing → **degraded** (a real fault, named);
      * nothing coming up and the queue has waited past the grace window → **degraded** (stalled).
    """
    rows = [h for h in hbs if h["component"] == "worker"]
    live = [h for h in rows if h["age_s"] < WORKER_STALE_S]
    n = len(live)
    claims = sum(int(h["detail"].get("claimed_this_loop", 0) or 0) for h in live)
    queued = (queues.get("ledger") or {}).get("queued", 0)
    pods = pods or []
    ready_pods = sum(1 for p in pods if p.get("ready"))
    crashing = [p for p in pods if p.get("reason") in WORKER_CRASH_REASONS
                or p.get("phase") == "Failed" or (p.get("restarts") or 0) >= WORKER_CRASH_RESTARTS]
    starting = [p for p in pods if p not in crashing and not p.get("ready")]
    stuck = queued_age_s is not None and queued_age_s > WORKER_SCALEUP_GRACE_S

    metrics: dict = {"live": n, "claims": claims}
    if pods:
        metrics["pods_ready"] = ready_pods
    if queued_age_s is not None and n == 0 and ready_pods == 0:
        metrics["queued_age_s"] = round(queued_age_s)

    # Healthy: a heartbeating worker, or a ready pod that's just busy (stale heartbeat mid-batch).
    if n > 0:
        return _comp("workers", "ok", f"{n} live · {claims} in-flight claims", metrics)
    if ready_pods:
        return _comp("workers", "ok", f"{ready_pods} pod(s) up · busy (no recent heartbeat)", metrics)

    # Nothing heartbeating and nothing ready — is it coming up, stalled, or broken?
    if crashing:
        why = crashing[0].get("reason") or "crash-looping"
        return _comp("workers", "degraded", f"{len(crashing)} pod(s) unhealthy · {why}", metrics)
    if starting:
        if stuck:
            return _comp("workers", "degraded",
                         f"{len(starting)} pod(s) stuck starting {_fmt_dur(queued_age_s)} · unschedulable?",
                         metrics)
        return _comp("workers", "scaling", f"{len(starting)} pod(s) starting · scaling up from 0", metrics)
    if queued > 0:
        if stuck:
            return _comp("workers", "degraded",
                         f"0 workers · queued {_fmt_dur(queued_age_s)}, none scheduled — KEDA not scaling?",
                         metrics)
        return _comp("workers", "scaling", "0 workers · KEDA activating (scaling from 0)", metrics)
    return _comp("workers", "idle", "0 workers · idle (scaled to 0)", metrics)


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
                css = p.status.container_statuses or []
                restarts = sum((cs.restart_count or 0) for cs in css)
                ready = all(cs.ready for cs in css) and bool(css)
                # waiting reason of the first not-ready container: ContainerCreating/PodInitializing
                # (a normal cold start) vs CrashLoopBackOff/ImagePullBackOff (a real fault).
                reason = next((cs.state.waiting.reason for cs in css
                               if cs.state and cs.state.waiting and cs.state.waiting.reason), None)
                pods.append({"name": p.metadata.name, "phase": p.status.phase, "ready": ready,
                             "restarts": restarts, "reason": reason, "node": p.spec.node_name,
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


# --- orphaned-sandbox reaper (leader-elected; driven by the orchestrator) ------------------------
# Agentic sandboxes are one ephemeral inspect-k8s-sandbox helm release per sample, torn down by
# Inspect when the sample finishes. If the owning worker dies mid-eval (SIGKILL on rollout / OOM /
# node drain) before that teardown runs, the release leaks — and on the small, hard-capped sandbox
# node pool a couple of leaks can wedge it (each sandbox ~fills a node), so every new sample's helm
# install then times out. This is the safety net that reclaims them; the leak source itself is
# tightened by giving the worker enough terminationGracePeriod to finish + tear down on drain.
#
# A sandbox's whole lifetime is bounded: helm install (≤ its install timeout, image pull included) +
# the per-sample wall-clock cap + teardown. A release older than that cap is therefore an orphan, and
# reaping by age can NEVER race a live sandbox — a real one is always younger than the cutoff.
SAMPLE_TIME_LIMIT_S = float(os.environ.get("EVAL_ENGINE_SAMPLE_TIME_LIMIT", "600"))
SANDBOX_REAP_AFTER_S = float(os.environ.get(
    "EVAL_ENGINE_SANDBOX_REAP_AFTER_SECONDS", str(2 * SAMPLE_TIME_LIMIT_S + 600)))  # ≈30m at defaults


def _orphan_releases(latest_age_s: dict[str, float], cutoff_s: float) -> list[str]:
    """Pure policy split: of {release: age_seconds}, the names old enough to be orphans. Sorted
    oldest-first so the most-stranded nodes are reclaimed first if we ever cap a sweep."""
    return [name for name, _ in sorted(latest_age_s.items(), key=lambda kv: kv[1], reverse=True)
            if cutoff_s <= latest_age_s[name]]


def reap_orphan_sandboxes(now: datetime | None = None) -> list[str]:
    """``helm uninstall`` sandbox releases in ``SANDBOX_NS`` older than ``SANDBOX_REAP_AFTER_S``,
    returning the reaped release names. No-ops (``[]``) when not in-cluster / the k8s client is absent
    (dev). Best-effort and self-isolating: every failure is logged, never raised — cleanup must never
    break the orchestrator tick that drives it."""
    try:
        from kubernetes import client, config
        config.load_incluster_config()
    except Exception:  # noqa: BLE001 — not in-cluster / client missing (local dev)
        return []
    core = client.CoreV1Api()
    try:
        # Helm 3 stores each release revision as a Secret labelled owner=helm, name=<release>.
        secrets = core.list_namespaced_secret(
            SANDBOX_NS, label_selector="owner=helm", timeout_seconds=4).items
    except Exception as e:  # noqa: BLE001
        print(f"[reaper] listing helm releases in {SANDBOX_NS} failed: {e}", flush=True)
        return []
    now = now or datetime.now(timezone.utc)
    # newest secret per release name = that release's current revision → its age.
    newest: dict[str, datetime] = {}
    for s in secrets:
        name = (s.metadata.labels or {}).get("name")
        ts = s.metadata.creation_timestamp
        if name and ts and (name not in newest or ts > newest[name]):
            newest[name] = ts
    ages = {name: (now - ts).total_seconds() for name, ts in newest.items()}
    reaped: list[str] = []
    for name in _orphan_releases(ages, SANDBOX_REAP_AFTER_S):
        try:
            r = subprocess.run(["helm", "uninstall", name, "-n", SANDBOX_NS, "--timeout", "120s"],
                               capture_output=True, text=True, timeout=150)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[reaper] helm uninstall {name!r} errored: {e}", flush=True)
            continue
        if r.returncode == 0:
            reaped.append(name)
            print(f"[reaper] uninstalled orphan sandbox {name!r} (age {_fmt_dur(ages[name])})", flush=True)
        else:
            print(f"[reaper] helm uninstall {name!r} failed: {(r.stderr or r.stdout).strip()[:200]}",
                  flush=True)
    return reaped


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
    workloads = (k8s or {}).get("workloads", []) if isinstance(k8s, dict) else []
    by_app = {w["app"]: w for w in workloads}

    # Active runs (fetched once, reused below). The oldest run still carrying queued samples tells us
    # how long work has actually been waiting — the signal that separates a normal KEDA scale-from-0
    # from a genuine stall when no worker is heartbeating yet.
    active = control.active_runs_detail()
    now = datetime.now(timezone.utc)
    waited = []
    for r in active:
        if (r.get("queued") or 0) > 0 and r.get("created_at"):
            try:
                waited.append((now - datetime.fromisoformat(r["created_at"])).total_seconds())
            except (TypeError, ValueError):
                pass
    queued_age_s = max(waited) if waited else None

    # heartbeat-derived components (cheap PG reads already in hand). probe_workers reconciles against
    # the K8s pod truth (phases/restarts) + how long work has waited so a busy worker reads as up, a
    # cold KEDA spin-up reads as `scaling`, and only a real fault/stall reads as `degraded`.
    worker_pods = by_app.get("eval-engine-worker", {}).get("pods")
    results["orchestrator"] = probe_orchestrator(hbs)
    results["workers"] = probe_workers(hbs, queues, worker_pods, queued_age_s)

    # Enrich app-backed components with the Kubernetes pod truth (ready/desired + restarts). Workers
    # are skipped for the auto-degrade: probe_workers already weighed the pod state holistically (a
    # ready<desired during scale-up is `scaling`, not a fault), so don't second-guess it here.
    for name, comp in results.items():
        w = by_app.get(COMPONENT_APP.get(name, ""))
        if w:
            comp["metrics"]["pods"] = f"{w['ready']}/{w['desired']}"
            comp["metrics"]["restarts"] = sum(p["restarts"] for p in w["pods"])
            if name != "workers" and w["desired"] and not w["ready"] and comp["status"] == "ok":
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
            for r in active
        ],
        "failures": [
            {**f, "logs_url": log_url(container="worker", run_id=f["run_id"], severity="ERROR")}
            for f in control.recent_failures(15)
        ],
        "audit": control.list_audit(15),
    }
