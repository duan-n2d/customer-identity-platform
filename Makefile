PY ?= python

.PHONY: install demo test test-fast lint fmt dbt report clean
install:
	$(PY) -m pip install -e ".[dev]"
demo:
	cip demo
test:
	$(PY) -m pytest -q
test-fast:
	$(PY) -m pytest -q -m "not slow"
lint:
	ruff check src tests && ruff format --check src tests
fmt:
	ruff format src tests
dbt:
	cip dbt
report:
	cip report
clean:
	rm -rf data results .pytest_cache .ruff_cache
