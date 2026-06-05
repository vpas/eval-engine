# eval-engine test + dev targets. Tests are layered (see tests/{unit,integration,e2e}/):
#   unit        — pure logic, no backends (fast)
#   integration — one module vs its real backend (Postgres + ClickHouse via infra/up.sh)
#   e2e         — full spine + agentic sandbox (also needs docker)
PY := .venv/bin/python
PYTEST := .venv/bin/pytest

.PHONY: install up down test test-unit test-int test-e2e

install:                     ## install the package + test deps into .venv
	$(PY) -m pip install -e '.[test,openrouter]'

up:                          ## start local Postgres + ClickHouse (docker)
	bash infra/up.sh

down:                        ## stop local Postgres + ClickHouse
	bash infra/down.sh

test-unit:                   ## fast unit tests, no backends needed
	$(PYTEST) -m unit

test-int: up                 ## integration tests (needs the backends)
	$(PYTEST) -m integration

test-e2e: up                 ## full-spine + sandbox tests
	$(PYTEST) -m e2e

test: up                     ## the whole suite
	$(PYTEST)
