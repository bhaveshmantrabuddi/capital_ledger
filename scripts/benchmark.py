#!/usr/bin/env python3
"""Reproducible synthetic HTTP deposit benchmark using only the Python stdlib.

Start the local server, then run:
    python scripts/benchmark.py --requests 100 --concurrency 8 --output benchmark.json

For a deployed API, pass --base-url and put a current access token in the
environment variable named by --token-env. This script never prints the token.
Every operation uses a distinct idempotency key. It creates one isolated account
unless --account-id is supplied, and each successful request adds amount-cents.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid


def request_json(base_url, method, path, payload=None, token=None, key=None, timeout=30):
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    request = Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {"error": {"code": "non_json_http_error"}}
        return exc.code, result


def positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def percentile(values, fraction):
    """Nearest-rank percentile; values are seconds, output is milliseconds."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000, 3)


def entity(payload, name):
    """Accept a named response entity or the direct JSON object."""
    if isinstance(payload, dict) and name in payload:
        return payload[name]
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--requests", type=positive_int, default=100)
    parser.add_argument("--concurrency", type=positive_int, default=8)
    parser.add_argument("--amount-cents", type=positive_int, default=1)
    parser.add_argument("--account-id", help="Existing account to mutate; default creates a fresh benchmark account")
    parser.add_argument("--dev-user", choices=("alice", "bob"), default="alice")
    parser.add_argument("--token-env", default="LEDGER_BENCH_TOKEN", help="Environment variable holding a bearer token")
    parser.add_argument("--timeout-seconds", type=positive_int, default=30)
    parser.add_argument("--output", type=Path, help="Write the report here; otherwise print JSON only")
    args = parser.parse_args()
    parsed = urlparse(args.base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        parser.error("--base-url must be an http(s) URL")
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1") and parsed.scheme != "https":
        parser.error("a deployed API must use HTTPS so its access token is not sent in cleartext")
    token = os.environ.get(args.token_env)
    if not token:
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            parser.error("a deployed API needs an access token in " + args.token_env)
        status, result = request_json(args.base_url, "POST", "/api/dev/login", {"user": args.dev_user}, timeout=args.timeout_seconds)
        if status != 200:
            raise RuntimeError("Local development login failed (HTTP %s)" % status)
        token = result.get("token") or result.get("access_token")
        if not token:
            raise RuntimeError("Local development login returned no token")

    health_status, health = request_json(args.base_url, "GET", "/api/health", timeout=args.timeout_seconds)
    account_id = args.account_id
    if account_id is None:
        status, result = request_json(args.base_url, "POST", "/api/accounts", {
            "name": "Benchmark " + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "full_name": "Synthetic Benchmark User",
            "date_of_birth": "1995-01-01",
            "initial_balance_cents": 0,
        }, token, str(uuid.uuid4()), args.timeout_seconds)
        if status not in (200, 201):
            error = result.get("error", {})
            raise RuntimeError("Benchmark account creation failed (HTTP %s, %s)" % (status, error.get("code", "unknown")))
        account = entity(result, "account")
        account_id = account.get("id") or account.get("account_id")
        if not account_id:
            raise RuntimeError("Account creation returned no account ID")

    before_status, before_payload = request_json(args.base_url, "GET", "/api/accounts/" + account_id, token=token, timeout=args.timeout_seconds)
    if before_status != 200:
        raise RuntimeError("Unable to read benchmark account (HTTP %s)" % before_status)
    before = entity(before_payload, "account").get("balance_cents")
    if not isinstance(before, int):
        raise RuntimeError("Account response has no integer balance_cents")

    def operation(index):
        started = time.perf_counter()
        key = str(uuid.uuid4())
        try:
            status, result = request_json(args.base_url, "POST", "/api/accounts/" + account_id + "/deposits", {
                "amount_cents": args.amount_cents,
                "description": "Synthetic benchmark operation %d" % index,
            }, token, key, args.timeout_seconds)
            if 200 <= status < 300:
                category = "success"
            else:
                error = result.get("error", {}) if isinstance(result, dict) else {}
                category = "http_%s:%s" % (status, error.get("code", "unknown"))
            return category, time.perf_counter() - started, key, index
        except (URLError, TimeoutError, OSError):
            return "transport_error", time.perf_counter() - started, key, index

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(executor.map(operation, range(args.requests)))
    elapsed = time.perf_counter() - start
    counts = Counter(category for category, _, _, _ in results)
    successes = counts.get("success", 0)
    after_status, after_payload = request_json(args.base_url, "GET", "/api/accounts/" + account_id, token=token, timeout=args.timeout_seconds)
    after = entity(after_payload, "account").get("balance_cents") if after_status == 200 else None
    expected = before + successes * args.amount_cents
    report = {
        "description": "Synthetic concurrent deposits into one account; unique idempotency key per request",
        "started_at_utc": started_at,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
            "api_host": parsed.hostname,
            "api_health_status": health_status,
            "api_health": {k: v for k, v in health.items() if k in ("status", "backend", "mode", "database")} if isinstance(health, dict) else {},
        },
        "workload": {"requests": args.requests, "concurrency": args.concurrency, "amount_cents": args.amount_cents, "account_id": account_id},
        "results": {
            "elapsed_seconds": round(elapsed, 6),
            "completed_requests_per_second": round(args.requests / elapsed, 3),
            "successful_requests_per_second": round(successes / elapsed, 3),
            "response_categories": dict(sorted(counts.items())),
            "all_request_latency_ms": {"p50": percentile([latency for _, latency, _, _ in results], 0.5), "p95": percentile([latency for _, latency, _, _ in results], 0.95)},
            "successful_request_latency_ms": {"p50": percentile([latency for category, latency, _, _ in results if category == "success"], 0.5), "p95": percentile([latency for category, latency, _, _ in results if category == "success"], 0.95)},
            "unsuccessful_requests": [{"category": category, "idempotency_key": key, "operation_index": index} for category, _, key, index in results if category != "success"],
        },
        "correctness": {"initial_balance_cents": before, "expected_balance_cents": expected, "observed_balance_cents": after, "balance_matches_successful_operations": after == expected},
        "limitations": ["Synthetic workload, not real users or production traffic", "Client and server placement affect latency", "Balance check assumes no other actor mutated this benchmark account", "Transport failures may have committed; investigate using recorded idempotency keys before interpreting mismatches"],
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print("Wrote benchmark report to " + str(args.output))
    else:
        print(encoded, end="")
    return 0 if after == expected and successes == args.requests else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, URLError, TimeoutError, OSError) as exc:
        print("Benchmark failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
