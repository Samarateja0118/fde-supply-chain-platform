.PHONY: install test lint demo local-demo benchmark clean

install:
	python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests

# Docker: everything, including Postgres, the mock ERP (with faults) and the review API on :8000
demo:
	docker compose build
	docker compose up -d --wait db erp      # erp waits for the one-shot `prepare` job
	docker compose run --rm --no-deps demo
	docker compose up -d --wait api
	@echo "review queue: http://localhost:8000/review   metrics: http://localhost:8000/metrics"

# No Docker: SQLite + in-process mock ERP
local-demo:
	.venv/bin/fde demo --database-url sqlite:///artifacts/fde.db

benchmark:
	.venv/bin/fde docs-benchmark --split dev --n 400 --seed 4
	.venv/bin/fde docs-benchmark --split holdout --n 400 --seed 4

clean:
	docker compose down -v; rm -rf artifacts data/generated
