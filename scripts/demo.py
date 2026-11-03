"""Host-side bounded Compose experiment (CPU, network latency, or network timeout).

Uses only Python's standard library.
"""

import argparse
import json
import math
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose"]
OPENER = build_opener(ProxyHandler({}))


def command(*args, timeout=90):
    result = subprocess.run(
        [*COMPOSE, *args], cwd=ROOT, text=True, capture_output=True, check=True, timeout=timeout
    )
    return result.stdout


CPU_CONTROL = ("worker", "lab.fault_cli", "/var/lib/lab/scenarios.jsonl")
NETWORK_CONTROL = (
    "network-fault-controller",
    "lab.network_fault_cli",
    "/var/lib/lab/network-scenarios.jsonl",
)
SLOW_FRACTION = (
    '1 - sum(rate(lab_http_request_duration_seconds_bucket{job="api",route="/work",le="0.25"}'
    '[30s])) / sum(rate(lab_http_request_duration_seconds_count{job="api",route="/work"}[30s]))'
)
TIMEOUT_FRACTION = (
    'sum(rate(lab_worker_calls_total{job="api",outcome="timeout"}[30s])) '
    '/ clamp_min(sum(rate(lab_worker_calls_total{job="api"}[30s])), 0.1)'
)
# Each scenario names its control channel, start arguments, the alert it is expected to fire,
# and the measured signal (with the alert's threshold) that must rise before that alert counts.
SCENARIOS = {
    "cpu": {
        "control": CPU_CONTROL,
        "start": (),
        "alert": "WorkerCpuPressure",
        "signal": 'rate(process_cpu_seconds_total{job="worker"}[15s])',
        "threshold": 0.30,
        "load_seconds": 120,
    },
    "latency": {
        "control": NETWORK_CONTROL,
        "start": ("--scenario", "latency", "--latency-ms", "600"),
        "alert": "ApiHighLatency",
        "signal": SLOW_FRACTION,
        "threshold": 0.25,
        "load_seconds": 180,
    },
    "timeout": {
        "control": NETWORK_CONTROL,
        "start": ("--scenario", "timeout"),
        "alert": "WorkerCallTimeouts",
        "signal": TIMEOUT_FRACTION,
        "threshold": 0.20,
        "load_seconds": 180,
    },
}
# User-facing impact recorded alongside every sample; never used to decide pass/fail.
IMPACT = {
    "api_p95_seconds": "histogram_quantile(0.95, sum by (le) "
    '(rate(lab_http_request_duration_seconds_bucket{job="api",route="/work"}[30s])))',
    "worker_p95_seconds": "histogram_quantile(0.95, sum by (le) "
    '(rate(lab_http_request_duration_seconds_bucket{job="worker",route="/work"}[30s])))',
    "api_error_fraction": 'sum(rate(lab_http_requests_total{job="api",route="/work",status=~"5.."}'
    '[30s])) / clamp_min(sum(rate(lab_http_requests_total{job="api",route="/work"}[30s])), 0.1)',
    "worker_cpu_cores": 'rate(process_cpu_seconds_total{job="worker"}[15s])',
}


def control(channel, action, *args):
    service, module, _ = channel
    output = command("exec", "-T", service, "python", "-m", module, action, *args)
    result = json.loads(output)
    if not result["ok"]:
        raise RuntimeError(result)
    return result


def query(base, expression):
    with OPENER.open(
        f"{base}/api/v1/query?{urlencode({'query': expression})}", timeout=5
    ) as response:
        data = json.load(response)
    if data["status"] != "success":
        raise RuntimeError(data)
    return data["data"]["result"]


def measure(base, expression):
    samples = query(base, expression)
    if len(samples) != 1 or not math.isfinite(float(samples[0]["value"][1])):
        raise RuntimeError(f"Metric is missing; a missing sample is not recovery: {expression}")
    return float(samples[0]["value"][1])


def impact(base):
    values = {}
    for name, expression in IMPACT.items():
        samples = query(base, expression)
        value = float(samples[0]["value"][1]) if len(samples) == 1 else math.nan
        # Undefined percentiles (no observations) are recorded as null, never as zero.
        values[name] = value if math.isfinite(value) else None
    return values


def wait_for(check, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {description}")


def one_run(base, artifact_dir, scenario="cpu"):
    spec = SCENARIOS[scenario]
    channel, threshold = spec["control"], spec["threshold"]
    run_key = uuid.uuid4().hex[:12]
    container_name = f"lab-demo-load-{run_key}"
    report = {
        "experiment_id": run_key,
        "scenario": scenario,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "signal": {"expression": spec["signal"], "threshold": threshold, "alert": spec["alert"]},
        "signal_samples": [],
    }
    load = None
    owns_fault = False
    if control(CPU_CONTROL, "status")["active"] or control(NETWORK_CONTROL, "status")["active"]:
        raise RuntimeError("An existing fault is active; wait or stop it before the demo")
    log_path = artifact_dir / f"{run_key}-load.log"
    load_seconds = spec["load_seconds"]
    try:
        with log_path.open("w") as log:
            load = subprocess.Popen(
                [
                    *COMPOSE,
                    *("run", "--rm", "--name", container_name, "load-generator"),
                    *("python", "scripts/generate_load.py", "--duration", str(load_seconds)),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            print(f"{run_key}: {scenario}: collecting 20 seconds of baseline", flush=True)
            time.sleep(20)
            if load.poll() is not None:
                raise RuntimeError(f"Load generator exited early; inspect {log_path}")
            if len(query(base, 'up{job=~"api|worker"} == 1')) != 2:
                raise RuntimeError("Both application scrape targets must be healthy")
            baseline = measure(base, spec["signal"])
            if baseline >= threshold or query(base, 'ALERTS{alertstate="firing"}'):
                raise RuntimeError(
                    "Baseline is not healthy; inspect metrics before injecting a fault"
                )
            report["baseline_signal"] = baseline
            report["baseline_impact"] = impact(base)
            report["fault"] = control(channel, "start", *spec["start"])["run"]
            owns_fault = True
            fault_started = time.monotonic()

            def firing():
                report["signal_samples"].append(
                    {
                        "elapsed_seconds": time.monotonic() - fault_started,
                        "value": measure(base, spec["signal"]),
                        **impact(base),
                    }
                )
                return bool(
                    query(base, f'ALERTS{{alertname="{spec["alert"]}",alertstate="firing"}}')
                )

            wait_for(firing, 50, f"{spec['alert']} firing")
            report["observed_detection_seconds"] = time.monotonic() - fault_started
            if max(x["value"] for x in report["signal_samples"]) <= threshold:
                raise RuntimeError("Alert fired without an observed signal increase")
            print(f"{run_key}: alert fired; waiting for automatic fault expiry", flush=True)
            wait_for(lambda: not control(channel, "status")["active"], 80, "automatic expiry")
            ended = time.monotonic()

            def recovered():
                return (
                    len(query(base, 'up{job=~"api|worker"} == 1')) == 2
                    and measure(base, spec["signal"]) < threshold
                    and not query(base, 'ALERTS{alertstate="firing"}')
                )

            wait_for(recovered, 45, "signal and alerts recovering with both services up")
            report["observed_recovery_seconds"] = time.monotonic() - ended
            report["recovery_signal"] = measure(base, spec["signal"])
            report["recovery_impact"] = impact(base)
            if channel is NETWORK_CONTROL and control(channel, "status").get("proxy_toxics"):
                raise RuntimeError("Proxy still has toxics after expiry")
            events = command("exec", "-T", channel[0], "cat", channel[2])
            report["ground_truth"] = [
                event
                for event in map(json.loads, events.splitlines())
                if event.get("run_id") == report["fault"]["run_id"]
            ]
            if not any(e.get("reason") == "duration_elapsed" for e in report["ground_truth"]):
                raise RuntimeError("Missing ground-truth automatic expiry record")
            if load.wait(timeout=load_seconds + 10) != 0:
                raise RuntimeError(f"Load generation failed; inspect {log_path}")
            command("run", "--rm", "smoke-test")
            report["status"] = "passed"
    except BaseException as exc:
        report["error"] = str(exc)
        raise
    finally:
        if owns_fault:
            try:
                control(channel, "stop")
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
                report.setdefault("cleanup_errors", []).append(str(exc))
        if load is not None and load.poll() is None:
            # Only remove the uniquely named disposable container created by this run.
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    timeout=15,
                    check=True,
                )
                load.wait(timeout=15)
            except (OSError, subprocess.SubprocessError) as exc:
                report.setdefault("cleanup_errors", []).append(str(exc))
                load.terminate()
        if report.get("cleanup_errors"):
            report["status"] = "failed"
        report["ended_at"] = datetime.now(UTC).isoformat()
        path = artifact_dir / f"{run_key}.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"{run_key}: {report['status']}; report: {path}", flush=True)
    if report.get("cleanup_errors"):
        raise RuntimeError(f"Experiment cleanup failed; inspect {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="cpu")
    args = parser.parse_args()
    # Ask Compose for its effective port mapping so .env overrides work too.
    config = json.loads(command("config", "--format", "json"))
    port = next(
        p["published"]
        for p in config["services"]["prometheus"]["ports"]
        if int(p["target"]) == 9090
    )
    base = f"http://127.0.0.1:{int(port)}"
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    command("run", "--rm", "smoke-test")
    for _ in range(args.runs):
        one_run(base, artifacts, args.scenario)


if __name__ == "__main__":
    main()
