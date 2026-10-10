.PHONY: up down clean test lint check-rules load inject-cpu-fault stop-fault fault-status smoke demo

DURATION ?= 60

up:
	docker compose up --build -d --wait
down:
	docker compose down
# Explicitly removes local monitoring history, Grafana state, and scenario records.
clean:
	docker compose down -v
test:
	uv run --frozen pytest -q
lint:
	uv run --frozen ruff check .
check-rules:
	docker compose run --rm --no-deps --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
	docker compose run --rm --no-deps --entrypoint promtool prometheus test rules /etc/prometheus/rules.test.yml
load:
	docker compose run --rm load-generator
inject-cpu-fault:
	docker compose exec -T worker python -m lab.fault_cli start --duration $(DURATION)
stop-fault:
	docker compose exec -T worker python -m lab.fault_cli stop
fault-status:
	docker compose exec -T worker python -m lab.fault_cli status
smoke:
	docker compose run --rm smoke-test
demo:
	python3 scripts/demo.py
