#!/usr/bin/env python3
"""Measure the real PostgreSQL ledger engine in a disposable generated schema.

This is an engine/SQL benchmark, not an HTTP, Lambda, DynamoDB or Aurora load test.
Set LEDGER_BENCH_DATABASE_URL to a dedicated PostgreSQL connection string, then:
    python scripts/benchmark_postgres.py --requests 200 --concurrency 8 --output report.json
The connection role needs CREATE SCHEMA privileges. The script creates and drops
only its own bench_<uuid> schema; it never initializes the public schema. The DSN
and connection exception messages are never included in output.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import statistics
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from ledger.database import Database
from ledger.engine import Ledger, LedgerError
from ledger.projection import LocalProjection


class BenchmarkInvariantError(RuntimeError):
    pass


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def checked_schema(value):
    if not re.fullmatch(r"bench_[0-9a-f]{32}", value):
        raise ValueError("Only a generated bench_<uuid> schema is permitted")
    return value


def percentile_ms(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000, 3)


def summarize(results, elapsed):
    counts = Counter(row["category"] for row in results)
    latencies = [row["seconds"] for row in results]
    return {
        "requests": len(results),
        "duration_seconds": round(elapsed, 6),
        "requests_per_second": round(len(results) / elapsed, 3) if elapsed else None,
        "successful_responses_per_second": round(counts["success"] / elapsed, 3) if elapsed else None,
        "categories": dict(sorted(counts.items())),
        "latency_ms": {
            "mean": round(statistics.mean(latencies) * 1000, 3) if latencies else None,
            "p50": percentile_ms(latencies, .50),
            "p95": percentile_ms(latencies, .95),
            "p99": percentile_ms(latencies, .99),
            "max": round(max(latencies) * 1000, 3) if latencies else None,
        },
    }


def verify_case(metrics, *, expected_successes, expected_rejections=0,
                actual_balance, expected_balance, transaction_count,
                expected_transactions, transaction_ids=None):
    """Fail closed: throughput alone is not a correctness result."""
    expected = {"success": expected_successes}
    if expected_rejections:
        expected["insufficient_funds"] = expected_rejections
    checks = {
        "outcomes_match": metrics["categories"] == expected,
        "balance_matches": actual_balance == expected_balance,
        "transaction_count_matches": transaction_count == expected_transactions,
    }
    if transaction_ids is not None:
        checks["one_transaction_for_replays"] = (
            bool(transaction_ids) and all(transaction_ids) and len(set(transaction_ids)) == 1
        )
    if not all(checks.values()):
        raise BenchmarkInvariantError("Workload correctness checks failed: " + json.dumps(checks))
    return checks


@contextmanager
def isolated_database(dsn):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    schema = checked_schema("bench_" + uuid.uuid4().hex)
    created = False
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            version = conn.execute("SELECT version(), current_setting('server_version_num')").fetchone()
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            created = True
        # Override any inherited search_path option. No application SQL can fall
        # through into public or another pre-existing application schema.
        scoped = make_conninfo(dsn, options="-csearch_path=" + schema)
        database = Database("postgres", scoped)
        database.bootstrap()
        yield database, {"version": version[0], "version_num": int(version[1]),
                         "driver": "psycopg", "driver_version": psycopg.__version__}
    finally:
        if created:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(checked_schema(schema))))


def timed_requests(operation, count, concurrency, seed):
    indexes = list(range(count))
    random.Random(seed).shuffle(indexes)

    def timed(index):
        started = time.perf_counter()
        try:
            result = operation(index)
            row = {"category": "success", "transaction_id": result.get("transaction_id")}
        except LedgerError as exc:
            row = {"category": exc.code}
        except Exception as exc:
            # Exception text can contain a connection string. Emit only the
            # class/SQLSTATE, sufficient to classify unexpected load failures.
            row = {"category": "unexpected_" + type(exc).__name__}
            if getattr(exc, "sqlstate", None):
                row["sqlstate"] = exc.sqlstate
        row["seconds"] = time.perf_counter() - started
        return row

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        results = list(executor.map(timed, indexes))
    return results, summarize(results, time.perf_counter() - started)


def synchronize_and_verify(service):
    service.worker_paused = False
    try:
        while True:
            batch = service.drain_outbox(500)
            if batch["failed"]:
                raise BenchmarkInvariantError("Snapshot delivery failed during verification")
            if batch["processed"] < 500:
                break
        report = service.reconcile()
        if not report["ok"] or report["projection_lag"] or report["pending_events"]:
            raise BenchmarkInvariantError("Ledger or projection reconciliation failed")
        with service.db.transaction(False) as tx:
            negative = tx.one("SELECT COUNT(*) AS n FROM accounts WHERE system_account=0 AND balance_cents<0")["n"]
        if negative:
            raise BenchmarkInvariantError("A customer account became negative")
        return {
            "ledger_balances_match": True,
            "transactions_have_two_balanced_entries": True,
            "customer_balances_nonnegative": True,
            "projection_versions_and_balances_match": True,
            "pending_outbox_events": report["pending_events"],
            "accounts_checked": report["accounts_checked"],
        }
    finally:
        service.worker_paused = True


def transaction_count(service, account_id):
    with service.db.transaction(False) as tx:
        return tx.one("SELECT COUNT(*) AS n FROM ledger_entries WHERE account_id=:id", {"id": account_id})["n"]


def create_account(service, name, balance):
    key = "create-" + re.sub(r"[^a-z0-9]+", "-", name.lower())
    return service.create_account("benchmark", key, {
        "name": name, "full_name": "Synthetic Benchmark User",
        "date_of_birth": "1995-01-01", "initial_balance_cents": balance,
    })["account"]["id"]


def run_benchmark(dsn, requests=200, concurrency=8, seed=2026):
    with isolated_database(dsn) as (database, pg):
        service = Ledger(database, LocalProjection(database))
        service.worker_paused = True
        cases = []
        account = create_account(service, "Hot account deposits", 0)
        results, metrics = timed_requests(lambda i: service.deposit(
            "benchmark", account, f"distinct-deposit-{i:08d}", {"amount_cents": 1}), requests, concurrency, seed)
        balance = service.get_account("benchmark", account)["balance_cents"]
        count = transaction_count(service, account)
        metrics.update(name="distinct_key_deposits", amount_cents=1,
                       final_balance_cents=balance, committed_transactions=count)
        metrics["checks"] = verify_case(metrics, expected_successes=requests,
            actual_balance=balance, expected_balance=requests,
            transaction_count=count, expected_transactions=requests)
        metrics["invariants"] = synchronize_and_verify(service)
        cases.append(metrics)

        attempts = max(32, concurrency * 4)
        account = create_account(service, "Overdraft contention", 10000)
        results, metrics = timed_requests(lambda i: service.withdraw(
            "benchmark", account, f"contended-withdraw-{i:08d}", {"amount_cents": 1000}), attempts, concurrency, seed)
        balance = service.get_account("benchmark", account)["balance_cents"]
        count = transaction_count(service, account)
        metrics.update(name="overdraft_contention", amount_cents=1000,
                       opening_balance_cents=10000, final_balance_cents=balance, committed_transactions=count)
        metrics["checks"] = verify_case(metrics, expected_successes=10,
            expected_rejections=attempts - 10, actual_balance=balance, expected_balance=0,
            transaction_count=count, expected_transactions=11)
        metrics["invariants"] = synchronize_and_verify(service)
        cases.append(metrics)

        account = create_account(service, "Concurrent same-key retry", 0)
        results, metrics = timed_requests(lambda i: service.deposit(
            "benchmark", account, "one-shared-deposit-key", {"amount_cents": 500}), attempts, concurrency, seed)
        balance = service.get_account("benchmark", account)["balance_cents"]
        count = transaction_count(service, account)
        metrics.update(name="same_key_replay", amount_cents=500,
                       final_balance_cents=balance, committed_transactions=count,
                       replayed_responses=attempts - 1)
        metrics["checks"] = verify_case(metrics, expected_successes=attempts,
            actual_balance=balance, expected_balance=500,
            transaction_count=count, expected_transactions=1,
            transaction_ids=[r.get("transaction_id") for r in results if r["category"] == "success"])
        metrics["invariants"] = synchronize_and_verify(service)
        cases.append(metrics)

        report = {
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "benchmark": "PostgreSQL ledger engine; not HTTP or AWS throughput",
            "environment": {"os": platform.system(), "os_release": platform.release(),
                "architecture": platform.machine(), "processor": platform.processor(),
                "logical_cpus": os.cpu_count(), "python": platform.python_version(), "postgresql": pg},
            "configuration": {"deposit_requests": requests, "concurrency": concurrency, "seed": seed,
                "connections": "fresh connection per engine transaction; no pool",
                "projection": "LocalProjection on the same PostgreSQL server; delivery and reconciliation excluded from timed workloads",
                "latency": "inside worker; excludes executor queue wait; wall throughput includes dispatch",
                "schema": "isolated generated schema, removed after run"},
            "workloads": cases,
            "all_checks_passed": True,
        }
    # This is assigned only after the context manager successfully drops its schema.
    report["schema_cleanup"] = "completed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn-env", default="LEDGER_BENCH_DATABASE_URL")
    parser.add_argument("--requests", type=positive_int, default=200)
    parser.add_argument("--concurrency", type=positive_int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error("set " + args.dsn_env + " to a dedicated PostgreSQL connection string")
    try:
        report = run_benchmark(dsn, args.requests, args.concurrency, args.seed)
    except Exception as exc:
        print(json.dumps({"all_checks_passed": False, "error_type": type(exc).__name__,
                          "sqlstate": getattr(exc, "sqlstate", None)}), file=sys.stderr)
        return 1
    output = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
