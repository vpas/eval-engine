"""Minimal plugin registry — prototype of docs/PLUGINS.md.

Harnesses return an Inspect ``Solver``; scorers return an Inspect ``Scorer``. Registration is
keyed by ``(kind, name, version)``; each plugin declares a Pydantic config model whose JSON
Schema would (in production) drive RunSpec validation and the launch-wizard form.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel


@dataclass
class Plugin:
    kind: str
    name: str
    version: str
    factory: Callable[..., Any]
    config_model: type[BaseModel]
    primary_metric: str | None = None
    description: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.name, self.version)


_REGISTRY: dict[tuple[str, str, str], Plugin] = {}


def _register(kind, name, version, config, primary_metric=None, description=""):
    def deco(fn):
        p = Plugin(kind, name, version, fn, config, primary_metric, description)
        _REGISTRY[p.key] = p
        return fn

    return deco


def harness(name, version, config, description=""):
    return _register("harness", name, version, config, description=description)


def scorer(name, version, config, primary_metric=None, description=""):
    return _register(
        "scorer", name, version, config, primary_metric=primary_metric, description=description
    )


def get(kind, name, version) -> Plugin:
    try:
        return _REGISTRY[(kind, name, version)]
    except KeyError:
        avail = ", ".join(f"{k[1]}@{k[2]}" for k in _REGISTRY if k[0] == kind) or "(none)"
        raise KeyError(f"no {kind} '{name}@{version}'. available: {avail}") from None


def build(kind: str, spec: dict):
    """Resolve ``{type, version, config}`` -> (instantiated Inspect object, Plugin)."""
    p = get(kind, spec["type"], spec.get("version", "1.0.0"))
    cfg = p.config_model(**(spec.get("config") or {}))
    return p.factory(cfg), p


def catalog() -> list[dict]:
    """What ``eval-engine plugins sync`` would upsert into the Postgres catalog."""
    return [
        {
            "kind": p.kind,
            "name": p.name,
            "version": p.version,
            "primary_metric": p.primary_metric,
            "description": p.description,
            "config_schema": p.config_model.model_json_schema(),
        }
        for p in _REGISTRY.values()
    ]
