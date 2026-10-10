"""Usage: docker compose exec -T worker python -m lab.fault_cli start --duration 60."""

import argparse
import json
import socket

from lab.fault import DEFAULT_DURATION, SOCKET_PATH, validate_duration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    args = parser.parse_args()
    try:
        duration = validate_duration(args.duration)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(SOCKET_PATH)
            client.sendall(
                json.dumps({"action": args.action, "duration": duration}).encode() + b"\n"
            )
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline(4096))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Fault control unavailable: {exc}\n")
    print(json.dumps(response))
    if not response["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
