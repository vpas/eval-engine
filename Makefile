# eval-engine test + dev targets. Tests are layered (see tests/{unit,integration,e2e}/):
#   unit        — pure logic, no backends (fast)
#   integration — one module vs its real backend (Postgres + ClickHouse, docker)
#   e2e         — full spine + agentic sandbox (also needs docker)
# The integration/e2e suites self-provision their backends (tests/conftest.py reuses a running stack
# or starts one), so no `up` prerequisite — plain `pytest` works. `up`/`down` are for local app dev.
PY := .venv/bin/python
PYTEST := .venv/bin/pytest

.PHONY: install up down test test-unit test-int test-e2e

install:                     ## install the package + test deps into .venv
	$(PY) -m pip install -e '.[test,openrouter]'

up:                          ## start local Postgres + ClickHouse for app dev (docker)
	bash infra/up.sh

down:                        ## stop local Postgres + ClickHouse
	bash infra/down.sh

test-unit:                   ## fast unit tests, no backends needed
	$(PYTEST) -m unit

test-int:                    ## integration tests (backends auto-provisioned)
	$(PYTEST) -m integration

test-e2e:                    ## full-spine + sandbox tests (backends auto-provisioned)
	$(PYTEST) -m e2e

test:                        ## the whole suite (backends auto-provisioned)
	$(PYTEST)
