# PostgreSQL concurrency benchmark

This measures the real Python ledger engine against PostgreSQL. It is **not an HTTP, Lambda, Aurora, DynamoDB, or AWS capacity benchmark**. The recorded run uses synthetic data on a local Windows machine and a fresh PostgreSQL connection for each engine transaction, without a connection pool.

The script creates a random `bench_<uuid>` SQL schema, sets its connection search path to that schema alone, and drops that same schema in `finally`. It never bootstraps the `public` schema. Use a dedicated development database and a role allowed to create schemas. Set the DSN through an environment variable; the script does not write connection strings or connection exception messages to reports.

## Recorded result

Measured November 1, 2026 with Python 3.13.0, psycopg 3.3.6, PostgreSQL 18.1, Windows 11 AMD64, and eight logical CPUs (Intel family 6, model 126). Full environment, counts, timings, and checks are in [benchmark-postgres-local.json](benchmark-postgres-local.json).

| Workload | Requests / workers | Outcomes | Responses/s | p50 / p95 / p99 latency (ms) |
| --- | --- | --- | ---: | ---: |
| Unique-key deposits to one account | 200 / 8 | 200 committed deposits | 15.720 | 486.275 / 688.992 / 772.889 |
| Concurrent withdrawals against fixed funds | 32 / 8 | 10 committed; 22 expected `insufficient_funds` | 26.019 | 207.741 / 535.128 / 586.775 |
| Concurrent requests sharing one idempotency key | 32 / 8 | 32 successful responses; exactly one committed deposit | 23.599 | 287.083 / 369.579 / 390.051 |

Response throughput includes expected rejections and successful replays. It must not be read as newly committed financial transactions per second. The JSON also reports successful responses per second and committed transaction counts. The withdrawal transaction count is 11 because it includes the initial account funding transaction.

For each workload, all ledger balances matched the sum of entries, each financial transaction had exactly two entries summing to zero, customer balances stayed nonnegative, and projection balances and versions matched after delivery. Pending outbox events ended at zero. The generated schema was removed successfully. The existing **14 PostgreSQL engine tests also passed**, with no failures, errors, or skips; see [benchmark-postgres-engine-tests.json](benchmark-postgres-engine-tests.json).

## Reproduce on Windows

From the repository root, install dependencies into your Python environment:

```powershell
python -m pip install -r requirements-dev.txt
```

Use an existing dedicated PostgreSQL development instance, or initialize a **new, empty** local cluster as below. Choose a different unused port or fresh directory if these are already occupied. Do not point `pg_ctl` at an unrelated database directory.

```powershell
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$pgData = Join-Path $PWD '.local\pg-benchmark'
& "$pgBin\initdb.exe" -D $pgData -U ledger_bench --auth-local=trust --auth-host=trust --encoding=UTF8 --locale=C
& "$pgBin\pg_ctl.exe" -D $pgData -l "$pgData\server.log" -o '-h 127.0.0.1 -p 55441' -w start
```

This disposable cluster accepts local connections without a password and listens only on loopback. Run the benchmark and PostgreSQL tests, then stop this same cluster:

```powershell
$env:LEDGER_BENCH_DATABASE_URL = 'host=127.0.0.1 port=55441 dbname=postgres user=ledger_bench connect_timeout=5'
try {
    python scripts/benchmark_postgres.py --requests 200 --concurrency 8 --seed 2026 --output benchmark-output.json
    $env:TEST_DATABASE_URL = $env:LEDGER_BENCH_DATABASE_URL
    $env:PYTHONPATH = "$PWD\backend;$PWD\tests"
    python -m unittest test_engine.PostgreSQLEngineTests -v
    python -m unittest discover -s tests -p test_benchmark_postgres.py -v
} finally {
    & "$pgBin\pg_ctl.exe" -D $pgData -m fast -w stop
}
```

The benchmark exits nonzero if any unexpected outcome, wrong balance, duplicate financial write, failed delivery, or reconciliation mismatch appears. It also exits nonzero if cleanup fails. `--dsn-env` selects a different connection environment variable. `--requests` controls the deposit count; the overdraft and retry cases each use `max(32, concurrency * 4)` requests. `--seed` fixes submission order; scheduler timing and generated identifiers are intentionally not deterministic.

## Timing and limits

Latencies measure individual engine calls inside a worker and exclude executor queue wait. Throughput uses total workload wall time, including dispatch. Reported percentiles use nearest rank. All cases target one customer account to exercise lock contention, including the shared funding account lock.

Outbox delivery is paused during the timed calls and drained afterward into `LocalProjection` on the same PostgreSQL server. That isolates financial write behavior; delivery and reconciliation are separately required to pass, but their time is excluded from the latency table. The benchmark does not exercise the deployed DynamoDB projection or scheduled AWS recovery path.

This is one small local run, without warm-up, pooling, multiple-machine traffic, or a capacity sweep. It demonstrates concurrent ledger correctness and supplies a repeatable performance baseline. It does not establish production capacity or promise these latencies on AWS.
