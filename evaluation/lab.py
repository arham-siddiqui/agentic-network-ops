"""The real Compose lab behind the small interface the trial runner needs.

Fault control goes through the same `docker compose exec` channels as `make demo`; telemetry
capture goes through the investigator's allowlisted, read-only source.
"""

import json
import subprocess
import time
import uuid

from agent.telemetry import PrometheusSource, capture
from scripts.demo import (
    COMPOSE,
    CPU_CONTROL,
    NETWORK_CONTROL,
    ROOT,
    SCENARIOS,
    command,
    control,
    measure,
    query,
)

CHANNELS = {"cpu": CPU_CONTROL, "network": NETWORK_CONTROL}


class ComposeLab:
    def __init__(self, prometheus_url: str) -> None:
        self.prometheus_url = prometheus_url
        self.source = PrometheusSource(prometheus_url)
        self.sleep = time.sleep

    def faults_active(self) -> bool:
        return any(control(channel, "status")["active"] for channel in CHANNELS.values())

    def idle(self) -> bool:
        """Both services scraped, no fault active, and no alert firing."""
        return (
            not self.faults_active()
            and len(query(self.prometheus_url, 'up{job=~"api|worker"} == 1')) == 2
            and not query(self.prometheus_url, 'ALERTS{alertstate="firing"}')
        )

    def start_load(self, seconds: int):
        name = f"lab-eval-load-{uuid.uuid4().hex[:12]}"
        process = subprocess.Popen(
            [
                *COMPOSE,
                *("run", "--rm", "--name", name, "load-generator"),
                *("python", "scripts/generate_load.py", "--duration", str(seconds)),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return name, process

    def stop_load(self, handle) -> None:
        name, process = handle
        if process.poll() is None:
            # Only remove the uniquely named disposable container created for this trial.
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
            process.wait(timeout=15)

    def inject(self, scenario) -> dict:
        return control(CHANNELS[scenario.control], "start", *scenario.start_args)["run"]

    def stop(self, scenario) -> None:
        control(CHANNELS[scenario.control], "stop")

    def active(self, scenario) -> bool:
        return control(CHANNELS[scenario.control], "status")["active"]

    def events(self, scenario, run_id: str) -> list[dict]:
        service, _, path = CHANNELS[scenario.control]
        lines = command("exec", "-T", service, "cat", path).splitlines()
        return [e for e in map(json.loads, lines) if e.get("run_id") == run_id]

    def recovered(self, scenario) -> bool:
        if scenario.demo is not None:
            spec = SCENARIOS[scenario.demo]
            if measure(self.prometheus_url, spec["signal"]) >= spec["threshold"]:
                return False
        return self.idle()

    def capture(self, lookback: int, incident_id: str):
        return capture(self.source, lookback_seconds=lookback, incident_id=incident_id)

    def environment(self) -> dict:
        def run(*args):
            try:
                return subprocess.run(
                    args, capture_output=True, text=True, timeout=30, check=True
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return None

        config = json.loads(command("config", "--format", "json"))
        return {
            "docker_version": run("docker", "version", "--format", "{{.Server.Version}}"),
            "compose_version": run("docker", "compose", "version", "--short"),
            "docker_cpus_memory": run("docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"),
            "images": sorted({s["image"] for s in config["services"].values() if "image" in s}),
            "service_limits": {
                name: {"cpus": s.get("cpus"), "mem_limit": s.get("mem_limit")}
                for name, s in config["services"].items()
            },
        }
