"""Entity registry — register + version + list + get for datasets / evals / models (FR1–3).

Backend-agnostic (uses db.control): runs on SQLite by default, or Postgres via
EVAL_ENGINE_BACKEND=postgres. Run: PYTHONPATH=. .venv/bin/python tests/test_registry.py
"""
from eval_engine import db
from eval_engine.models import DatasetSpec, EvalSpec, ModelSpec, PluginRef


def test_registry():
    db.init()
    c = db.control

    # dataset: register v1 then v2 → "latest" resolves to v2, but v1 is still pinnable
    c.register_entity("dataset", "capitals", 1,
                      DatasetSpec(id="capitals", uri="examples/qa.jsonl").model_dump(), "me@x")
    c.register_entity("dataset", "capitals", 2,
                      DatasetSpec(id="capitals", version=2, uri="examples/qa2.jsonl").model_dump(), "me@x")
    latest = c.get_entity("dataset", "capitals")
    assert latest["version"] == 2 and latest["body"]["uri"] == "examples/qa2.jsonl", latest
    assert c.get_entity("dataset", "capitals", 1)["body"]["uri"] == "examples/qa.jsonl", "v1 not pinnable"

    # list returns the latest version per id
    ids = {e["id"]: e["version"] for e in c.list_entities("dataset")}
    assert ids.get("capitals") == 2, ids

    # eval bundle + model target
    c.register_entity("eval", "capitals_qa", 1, EvalSpec(
        id="capitals_qa", dataset="capitals",
        default_harness=PluginRef(type="single_turn"),
        default_scorers=[PluginRef(type="includes")]).model_dump(), "me@x")
    c.register_entity("model", "gpt-4o-mini-prod", 1, ModelSpec(
        id="gpt-4o-mini-prod", provider="openrouter", model_id="openai/gpt-4o-mini").model_dump(), "me@x")

    assert c.get_entity("eval", "capitals_qa")["body"]["dataset"] == "capitals"
    assert c.get_entity("model", "gpt-4o-mini-prod")["body"]["model_id"] == "openai/gpt-4o-mini"
    assert c.get_entity("dataset", "does-not-exist") is None
    print("registry ✓  register+version (latest+pinned) ✓  list ✓  get dataset/eval/model ✓ (FR1–3)")


def test_dataset_snapshot():
    """A dataset is content-addressed at registration: snapshot the bytes to immutable storage keyed
    by their hash (write-once / idempotent), and the snapshot loads back to the same samples (FR1, §13)."""
    from pathlib import Path

    from eval_engine import datasets

    h, snap = datasets.snapshot("examples/qa.jsonl")
    assert h and (snap.startswith("gs://") or Path(snap).exists()), (h, snap)
    assert h in snap, "snapshot key not content-addressed"
    # idempotent: same content → same hash + same key (write-once)
    assert datasets.snapshot("examples/qa.jsonl") == (h, snap)
    # the snapshot loads back to the same 3 samples, and its content hash matches
    ds, ch = datasets.load_jsonl(snap)
    assert len(ds.samples) == 3 and ch == h, (len(ds.samples), ch, h)
    print("snapshot ✓  content-addressed ✓  idempotent ✓  loads back to same samples ✓ (FR1/§13)")


def test_multiple_choice_plugins():
    """The multiple_choice harness + choice scorer register, and the loader reads a `choices` list
    and letter `target` into the Inspect Sample (FR for the MC eval shape; DESIGN §7)."""
    from eval_engine import builtins, datasets, plugins  # noqa: F401  builtins populates the registry

    cat = {(p["kind"], p["name"]) for p in plugins.catalog()}
    assert ("harness", "multiple_choice") in cat and ("scorer", "choice") in cat, cat
    ds, _ = datasets.load_jsonl("examples/mcq.jsonl")
    s = ds.samples[0]
    assert s.choices == ["London", "Paris", "Berlin", "Madrid"] and s.target == "B", (s.choices, s.target)
    print(f"multiple_choice ✓  choice scorer ✓  loader reads choices ✓ ({len(ds.samples)} MC samples)")


if __name__ == "__main__":
    test_registry()
    test_dataset_snapshot()
    test_multiple_choice_plugins()
    print("ALL PASS ✓")
