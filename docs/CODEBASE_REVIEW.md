# Codebase Review — June 2026

A full-codebase review: design, structure, correctness, security, tests, frontend, and
deploy/infra. Scope is the whole repo (`eval_engine/`, `frontend/`, `deploy/`, `infra/`, `tests/`,
`tools/`). Findings are ordered by severity within each section; each has a file:line anchor.
Items already deferred in `docs/FUTURE.md` (tenancy enforcement, gateway-canonical cost, gVisor)
are noted but not re-litigated.

**Overall verdict.** This is an unusually well-engineered prototype. The architecture is sound and
honestly documented: Inspect-native kernel, a real `FOR UPDATE SKIP LOCKED` ledger, ack-before-flip
commit with ReplacingMergeTree dedup as the idempotency backstop, leader election with stale-lock
reaping, and a test suite that proves the concurrent claim protocol against real Postgres rather
than mocks. The docs-to-code traceability (every mechanism cites its design section) is exemplary.
The findings below are mostly hardening and polish, with a handful of real correctness/security
items at the top — none of which undermine the core design.

---

## 1. Security (highest-priority findings)

### 1.1 HIGH — `/transcript` allows arbitrary file read on the API pod

`GET /transcript?uri=...` (`api.py:191-197`) passes the caller's URI to
`runner.get_transcript` (`runner.py:294-306`), which only validates **`gs://` URIs** (must match
our bucket). Any non-`gs://` URI falls through to `storage.exists()` / `storage.read_bytes()` —
fsspec treats a bare path as the local filesystem, so an authenticated user can fetch any file
readable in the API container, e.g. `?