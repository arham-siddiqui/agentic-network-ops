"""Host-side bounded Compose experiment. Uses only Python's standard library."""

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


def fault(action):
    result = json.loads(command("exec", "-T", "worker", "python", "-m", "lab.fault_cli", action))
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


def cpu_usage(base):
    samples = query(base, 'rate(process_cpu_seconds_total{job="worker"}[15s])')
    if len(samples) != 1 or not math.isfinite(float(samples[0]["value"][1])):
        raise RuntimeError("Worker CPU metric is missing; a missing sample is not recovery")
    return float(samples[0]["value"][1])


def wait_for(check, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {description}")


def one_run(base, artifact_dir):
    run_key = uuid.uuid4().hex[:12]
    container_name = f"lab-demo-load-{run_key}"
    report = {
        "experiment_id": run_key,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "cpu_samples": [],
    }
    load = None
    owns_fault = False
    if fault("status")["active"]:
        raise RuntimeError("An existing fault is active; wait or stop it before the demo")
    log_path = artifact_dir / f"{run_key}-load.log"
    try:
        with log_path.open("w") as log:
            load = subprocess.Popen(
                [*COMPOSE, "run", "--rm", "--name", container_name, "load-generator"],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            print(f"{run_key}: collecting 20 seconds of baseline", flush=True)
            time.sleep(20)
            if load.poll() is not None:
                raise RuntimeError(f"Load generator exited early; inspect {log_path}")
            if len(query(base, 'up{job=~"api|worker"} == 1')) != 2:
                raise RuntimeError("Both application scrape targets must be healthy")
            baseline = cpu_usage(base)
            if baseline >= 0.30 or query(base, 'ALERTS{alertstate="firing"}'):
                raise RuntimeError(
                    "Baseline is not healthy; inspect metrics before injecting a fault"
                )
            report["baseline_cpu_cores"] = baseline
            report["fault"] = fault("start")["run"]
            owns_fault = True
            fault_started = time.monotonic()

            def firing():
                cpu = cpu_usage(base)
                report["cpu_samples"].append(
                    {"elapsed_seconds": time.monotonic() - fault_started, "cores": cpu}
                )
                return bool(
                    query(base, 'ALERTS{alertname="WorkerCpuPressure",alertstate="firing"}')
                )

            wait_for(firing, 50, "WorkerCpuPressure firing")
            report["observed_detection_seconds"] = time.monotonic() - fault_started
            if max(x["cores"] for x in report["cpu_samples"]) <= 0.30:
                raise RuntimeError("Alert fired without an observed CPU increase")
            print(f"{run_key}: alert fired; waiting for automatic fault expiry", flush=True)
            wait_for(lambda: not fault("status")["active"], 80, "automatic fault expiry")
            ended = time.monotonic()

            def recovered():
                return (
                    len(query(base, 'up{job=~"api|worker"} == 1')) == 2
                    and cpu_usage(base) < 0.30
                    and not query(base, 'ALERTS{alertstate="firing"}')
                )

            wait_for(recovered, 45, "CPU and alerts recovering with both services up")
            report["observed_recovery_seconds"] = time.monotonic() - ended
            report["recovery_cpu_cores"] = cpu_usage(base)
            events = command("exec", "-T", "worker", "cat", "/var/lib/lab/scenarios.jsonl")
            report["ground_truth"] = [
                json.loads(line)
                for line in events.splitlines()
                if json.loads(line)["run_id"] == report["fault"]["run_id"]
            ]
            if not any(e.get("reason") == "duration_elapsed" for e in report["ground_truth"]):
                raise RuntimeError("Missing ground-truth automatic expiry record")
            if load.wait(timeout=130) != 0:
                raise RuntimeError(f"Load generation failed; inspect {log_path}")
            command("run", "--rm", "smoke-test")
            report["status"] = "passed"
    except BaseException as exc:
        report["error"] = str(exc)
        raise
    finally:
        if owns_fault:
            try:
                fault("stop")
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
        one_run(base, artifacts)


if __name__ == "__main__":
    main()
