.PHONY: up down clean test lint check-rules load inject-cpu-fault stop-fault fault-status smoke demo \
	inject-network-fault stop-network-fault network-fault-status network-demo investigate \
	evaluate evaluate-score live-demo integration telemetry worker-outage

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
# Stops the worker container for DURATION seconds, then always restarts it (Ctrl-C restarts early).
worker-outage:
	python3 scripts/worker_outage.py --duration $(DURATION)
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
# Labelled trials (healthy, cpu, latency, timeout, worker_down x REPS, ~3.3 min each).
# On macOS, wrap long runs in `caffeinate -dims`: host sleep corrupts trial timings.
REPS ?= 3
evaluate:
	uv run --frozen python -m evaluation.run trials --reps $(REPS)
# Re-score a saved run: make evaluate-score RUN=artifacts/evaluation/<timestamp>
evaluate-score:
	uv run --frozen python -m evaluation.run score $(RUN)
# Demo site with a read-only live view of the running lab (http://127.0.0.1:8088).
live-demo:
	python3 -m scripts.live_bridge
# Read-only tool checks against the running lab (generates 30 /work requests first).
integration:
	LAB_INTEGRATION=1 uv run --frozen pytest -q tests/test_tools_integration.py
# Inspect telemetry the way a future agent will: make telemetry SERVICE=worker WINDOW=300
SERVICE ?= worker
WINDOW ?= 300
telemetry:
	uv run --frozen python -m agent.tools metrics $(SERVICE) --window $(WINDOW)
	uv run --frozen python -m agent.tools logs $(SERVICE) --window $(WINDOW) --limit 10
	uv run --frozen python -m agent.tools alerts
