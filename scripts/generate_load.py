"""Bounded load against the fixed Compose API service; no arbitrary target option."""

import argparse
import asyncio
import json
import math
import time

import httpx

TARGET = "http://api:8000/work"


def validate_config(duration: float, rate: float, concurrency: int) -> None:
    if not math.isfinite(duration) or not 1 <= duration <= 600:
        raise ValueError("duration must be between 1 and 600 seconds")
    if not math.isfinite(rate) or not 0.1 <= rate <= 50:
        raise ValueError("rate must be between 0.1 and 50 requests/second")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 8
    ):
        raise ValueError("concurrency must be between 1 and 8")


async def generate(duration: float, rate: float, concurrency: int) -> dict:
    validate_config(duration, rate, concurrency)
    counts = {"success": 0, "errors": 0, "skipped": 0}
    pending: set[asyncio.Task] = set()
    async with httpx.AsyncClient(
        timeout=3,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=concurrency),
    ) as client:

        async def request():
            try:
                response = await client.get(TARGET)
                counts["success" if response.status_code == 200 else "errors"] += 1
            except httpx.HTTPError:
                counts["errors"] += 1

        started = time.monotonic()
        deadline = started + duration
        while time.monotonic() < deadline:
            if len(pending) < concurrency:
                task = asyncio.create_task(request())
                pending.add(task)
                task.add_done_callback(pending.discard)
            else:
                counts["skipped"] += 1
            # No catch-up bursts or unbounded task queue if the API slows down.
            await asyncio.sleep(min(1 / rate, max(0, deadline - time.monotonic())))
        if pending:
            await asyncio.gather(*pending)
    return {
        **counts,
        "duration_seconds": duration,
        "requested_rate": rate,
        "concurrency": concurrency,
        "elapsed_seconds": time.monotonic() - started,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--rate", type=float, default=10)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    try:
        result = asyncio.run(generate(args.duration, args.rate, args.concurrency))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result), flush=True)
    if not result["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
