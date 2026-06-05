"""Integration layer: one module against its real backend (Postgres/ClickHouse) in isolation.
The autouse fixture truncates the control tables before each test (via clean_db in the root conftest).
Auto-marked `integration` by the collection hook.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate(clean_db):
    yield
