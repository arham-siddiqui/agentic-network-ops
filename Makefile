.PHONY: up down clean test lint check-rules load inject-cpu-fault stop-fault fault-status smoke demo \
	inject-network-fault stop-network-fault network-fault-status network-demo investigate

DURATION ?= 60
SCENARIO ?= latency
LATENCY_MS ?= 600
NETWORK_FAULT = docker compose exec -T network-fault-controller python -m lab.network_fault_cli

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
# SCENARIO=latency (LATENCY_MS, default 600) or SCENARIO=timeout; both expire after DURATION.
inject-network-fault:
ifeq ($(SCENARIO),latency)
	$(NETWORK_FAULT) start --scenario latency --duration $(DURATION) --latency-ms $(LATENCY_MS)
else
	$(NETWORK_FAULT) start --scenario $(SCENARIO) --duration $(DURATION)
endif
# Also clears toxics left behind by an interrupted run.
stop-network-fault:
	$(NETWORK_FAULT) stop
network-fault-status:
	$(NETWORK_FAULT) status
network-demo:
	python3 scripts/demo.py --scenario $(SCENARIO)
# Read-only: captures allowlisted telemetry from the local Prometheus and writes a report.
investigate:
	uv run --frozen python -m agent.cli investigate --lookback 120
