"""E2E layer: the full spine (launch → admit → drain → finalize) and the agentic sandbox.
The autouse fixture truncates the control tables before each test. Auto-marked `e2e`.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate(clean_db):
    yield
