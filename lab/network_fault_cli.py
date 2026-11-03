"""Usage: docker compose exec -T network-fault-controller python -m lab.network_fault_cli
start --scenario latency --duration 60 --latency-ms 600."""

import argparse
import json
import socket

from lab.fault import DEFAULT_DURATION, validate_duration
from lab.network_fault import SCENARIOS, SOCKET_PATH, build_toxic


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--scenario", choices=SCENARIOS, default="latency")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--latency-ms", type=int)
    parser.add_argument("--jitter-ms", type=int)
    args = parser.parse_args()
    request: dict = {"action": args.action}
    try:
        if args.action == "start":
            request["scenario"] = args.scenario
            request["duration"] = validate_duration(args.duration)
            for key in ("latency_ms", "jitter_ms"):
                if getattr(args, key) is not None:
                    request[key] = getattr(args, key)
            build_toxic(args.scenario, request.get("latency_ms"), request.get("jitter_ms"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            # Starting and stopping may wait on the proxy admin API with bounded retries.
            client.settimeout(20)
            client.connect(SOCKET_PATH)
            client.sendall(json.dumps(request).encode() + b"\n")
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline(8192))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Network fault control unavailable: {exc}\n")
    print(json.dumps(response))
    if not response["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
