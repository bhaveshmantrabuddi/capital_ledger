"""Benchmark guard rails; real load and engine tests use a disposable PostgreSQL cluster."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("benchmark_postgres", Path(__file__).resolve().parents[1] / "scripts/benchmark_postgres.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class PostgresBenchmarkTests(unittest.TestCase):
    def test_schema_guard_rejects_existing_names_and_injection(self):
        for name in ("public", "accounts", "bench_abc", "bench_" + "a" * 32 + ";DROP SCHEMA public"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                benchmark.checked_schema(name)
        self.assertEqual("bench_" + "a" * 32, benchmark.checked_schema("bench_" + "a" * 32))

    def test_invariants_reject_high_throughput_with_bad_balance_or_unexpected_errors(self):
        for metrics, balance in (({"categories": {"success": 10}}, 1),
                                 ({"categories": {"success": 9, "unexpected_ConnectionError": 1}}, 10)):
            with self.subTest(metrics=metrics), self.assertRaises(benchmark.BenchmarkInvariantError):
                benchmark.verify_case(metrics, expected_successes=10,
                    actual_balance=balance, expected_balance=10,
                    transaction_count=10, expected_transactions=10)

    def test_replays_must_have_one_transaction_identifier(self):
        for identifiers in (["different-1", "different-2"], [None, None], []):
            with self.subTest(identifiers=identifiers), self.assertRaises(benchmark.BenchmarkInvariantError):
                benchmark.verify_case({"categories": {"success": 2}}, expected_successes=2,
                    actual_balance=500, expected_balance=500, transaction_count=1,
                    expected_transactions=1, transaction_ids=identifiers)

    def test_cli_never_prints_connection_exception_or_dsn(self):
        secret = "postgresql://user:do-not-print@example.invalid/database"
        captured = io.StringIO()
        with patch.dict(os.environ, {"LEDGER_BENCH_DATABASE_URL": secret}), \
             patch.object(benchmark, "run_benchmark", side_effect=RuntimeError(secret)), \
             contextlib.redirect_stderr(captured):
            status = benchmark.main([])
        self.assertEqual(1, status)
        self.assertNotIn(secret, captured.getvalue())
        self.assertEqual("RuntimeError", json.loads(captured.getvalue())["error_type"])


if __name__ == "__main__":
    unittest.main()
