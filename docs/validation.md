# Validation evidence

## Publication checks - November 1, 2026

The publication export ran **59 tests in 12.958 seconds**: **45 passed and 14 PostgreSQL integration tests were skipped** because no dedicated disposable PostgreSQL instance was configured for this run. The AWS boundary module includes 15 tests, including the regression for a null optional JWT scopes field. Frontend JavaScript and local configuration syntax checks passed.

## Live AWS demonstration - October 31, 2026

Verified the deployed S3/CloudFront website, Cognito sign-in, API Gateway JWT and required scope configuration, separate customer/transaction Lambda handlers, IAM execution permissions, Aurora Express IAM/TLS connection, and DynamoDB customer records. A fictional account was created with $100, received a $25 deposit, and completed a $10 withdrawal. Its $115 balance persisted after a page reload; a $200 withdrawal was rejected without changing the balance. A read-only Aurora query returned the three successful transactions, and DynamoDB held 11500 cents at version 3 for that same account. A repeated earlier withdrawal test returned the original transaction ID and unchanged balance/version, verifying a live idempotent replay.

The narrated demonstration includes actual AWS console configuration views and relevant code. Recordings and account-specific raw evidence are kept outside the source repository. These checks establish the demonstrated workflow, not AWS load capacity, a live service-outage recovery test, or deployed scheduled workers. The live setup has two HTTP Lambdas and post-commit projection attempts; scheduled outbox processing and reconciliation remain optional.

## Aurora Express setup - October 30, 2026

Verified a live, hostname- and certificate-checked TLS connection to the existing
Aurora PostgreSQL 17.9 Express cluster using temporary IAM credentials. The
account plan was checked as FREE and ACTIVE before schema initialization. The
project's six tables and immutable-history triggers were initialized in the
cluster's `postgres` database, with a zero-balance simulation funding account.
No customer financial operations were created by this bootstrap.

The IAM connector tests and existing local suite ran with 58 tests discovered:
44 passed and 14 dedicated local PostgreSQL integration tests were skipped
because no disposable local PostgreSQL instance was configured for this run.
The new Linux ARM64 Python 3.12 Lambda bundle includes a matching PostgreSQL
driver; its ELF architecture and ZIP integrity were checked. At this historical checkpoint the bundle had not yet been exercised inside Lambda. The October 31 live verification above supersedes that pending status.

## Free local edition update - October 30, 2026

All 12 HTTP integration tests passed in 2.964 seconds through the new
`start-local.py` launcher, with no dependency installation for the server. The
tests start the app from outside the project directory, remove inherited
`PYTHONPATH`, and deliberately set an invalid external `DATABASE_URL`. The
health endpoint confirms the app still runs in local mode on SQLite. Transfers,
ownership checks, idempotency, deposits/withdrawals, and paused-worker recovery
passed. JavaScript syntax checking and launcher `--help` also passed.

This update makes local mode the default and clarifies local summary labels. It
does not establish an AWS deployment or add new PostgreSQL benchmark evidence.

## Recorded checks

The final combined run passed **all 54 tests in 68.880 seconds** on October 20, 2026, with real PostgreSQL 18 available and no skipped tests. The table below separates the types of evidence; it does not imply AWS was deployed.

| Check | Result |
|---|---|
| Financial engine tests across SQLite and real PostgreSQL 18 | 28 passed |
| Local HTTP integration tests | 12 passed |
| Mocked AWS adapter and Lambda boundary tests | 14 passed |
| CloudFormation template lint | `cfn-lint` 1.57.2, exit code 0 |
| Frontend JavaScript and deployment PowerShell syntax | Passed |
| Browser walkthrough at desktop and 390px mobile widths | Passed; no horizontal page overflow |

Browser checks exercised account creation, deposits, withdrawals, cross-user transfers, and paused-worker recovery. Screenshots are saved in `frontend/preview.jpg`, `frontend/preview-mobile.jpg`, and `frontend/preview-recovery.jpg`. Cognito sign-in was subsequently verified in the October 31 AWS demonstration above.

## HTTP integration tests

The 12 tests in `tests/test_api.py` passed on October 20, 2026 against a temporary local HTTP server and isolated SQLite database. They exercise real HTTP requests rather than bypassing the router.

Coverage includes required customer creation/deposit/withdrawal behavior, authentication failures, forged bearer tokens, cross-user account access, transfer routing, strict integer amounts, idempotency conflicts and replay, overdraft rejection, CORS restrictions, and paused delivery followed by synchronization recovery.

Run the API tests from the project root with `backend/` on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "$PWD/backend"
python -m unittest discover -s tests -p test_api.py -v
```

The test suite launches and stops its own temporary server. The authoring sandbox blocks loopback client connections, so its run required execution outside that network sandbox. This is an environment restriction; no external network service is used by these tests.

These HTTP tests use SQLite. Use the separate PostgreSQL engine tests to evaluate PostgreSQL locking and concurrency semantics. Passing SQLite tests does not verify Aurora, DynamoDB, IAM, Cognito, or a deployed AWS website.

## AWS boundary tests

The 14 tests in `tests/test_aws_boundaries.py` passed without any AWS calls. They verify monotonic DynamoDB snapshot writes, propagation of retryable delivery failures, Decimal normalization, explicit Data API transaction IDs, commit/rollback boundaries, disabled SDK statement retries, verified JWT context and scope requirements, rejection of header-only forged tokens, separation of the customer and transaction handlers, absence of development routes in AWS, and fail-closed authentication configuration.

Run them with:

```powershell
$env:PYTHONPATH = "$PWD/backend"
python -m unittest discover -s tests -p test_aws_boundaries.py -v
```

The SDK-configuration test requires optional `boto3` and is skipped when it is not installed. All 14 tests ran in the recorded environment with `boto3` installed. SDK-shaped fakes establish the code's wire contract; they do not establish live AWS service behavior or IAM permission correctness.

## Recorded local benchmark

The actual output is saved in [`benchmark-local.json`](benchmark-local.json). It records the timestamp, environment, database backend, operation count, concurrency, response categories, initial and final balances, throughput, and latency percentiles. Read the saved report for measured values; a later run replaces those measurements rather than claiming one result applies to every revision.

Percentiles use the nearest-rank method. The measurements include HTTP handling, database work, and the attempt to synchronize snapshots. The workload deliberately concentrates writes on one account; it is not representative of a distributed set of customer accounts. Client and server placement, database mode, machine load, and synchronous delivery attempts all affect these numbers.

To reproduce with a running local server:

```powershell
python scripts/benchmark.py --requests 100 --concurrency 8 --output benchmark.json
```

The script creates a fresh fictional account unless `--account-id` is provided. Every successful request mutates that account. It reports actual response categories, throughput, latency percentiles, and a final balance check; it exits unsuccessfully if any operation fails or the balance differs from the count of successful operations. Transport failures can be ambiguous because the server may commit before the connection is lost; the report saves failed-request idempotency keys for investigation.

For a deployed API, set a current access token in `LEDGER_BENCH_TOKEN` and supply `--base-url`. Do not commit the token. A new run generates a new measured report rather than claiming the saved local results apply to AWS.

## Evidence boundaries

The historical tests and benchmark above describe their actual environments and dates. The October 31 section records the deployed AWS workflow, while the November 1 section records the publication test run. The optional SAM path and scheduled worker deployment are not represented as live-tested resources.
