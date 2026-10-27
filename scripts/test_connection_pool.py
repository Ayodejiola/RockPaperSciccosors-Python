import argparse
import asyncio
import json
import re
import statistics
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Request to {url} failed: {exc}") from exc


async def run(args: argparse.Namespace) -> None:
    health = await asyncio.to_thread(request_json, f"{args.url}/health/ready")
    if health.get("status") != "ready":
        raise RuntimeError(f"API is not ready: {health}")

    async def create_game() -> float:
        started = time.perf_counter()
        await asyncio.to_thread(
            request_json,
            f"{args.url}/api/games",
            "POST",
            {"upper_bound": 100},
        )
        return time.perf_counter() - started

    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded_request() -> float:
        async with semaphore:
            return await create_game()

    durations = await asyncio.gather(*(bounded_request() for _ in range(args.requests)))
    async def read_stats() -> dict:
        return await asyncio.to_thread(request_json, f"{args.url}/api/stats")

    cache_results = await asyncio.gather(*(read_stats() for _ in range(args.concurrency)))
    metrics = await asyncio.to_thread(
        lambda: urlopen(f"{args.url}/metrics", timeout=10).read().decode()
    )

    def metric(name: str) -> float:
        match = re.search(rf"^{name}\s+([-+]?[0-9]*\.?[0-9]+)$", metrics, re.MULTILINE)
        if match is None:
            raise RuntimeError(f"Missing Prometheus metric: {name}")
        return float(match.group(1))

    capacity = metric("app_db_pool_capacity")
    peak_checked_out = metric("app_db_pool_checked_out_peak")
    redis_capacity = metric("app_redis_pool_capacity")
    if peak_checked_out > capacity:
        raise RuntimeError(f"Pool use {peak_checked_out} exceeded configured capacity {capacity}")

    print(f"Requests: {args.requests}; concurrency: {args.concurrency}")
    print(f"Mean request time: {statistics.mean(durations) * 1000:.1f} ms")
    print(f"DB pool: peak checked-out={peak_checked_out:.0f}, per-process cap={capacity:.0f}")
    print(
        f"Redis pool: {len(cache_results)} concurrent cache requests succeeded; "
        f"per-process cap={redis_capacity:.0f}"
    )
    print("Connection-pool capacity check passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Exercise the API and check its DB pool metrics.")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=30)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
