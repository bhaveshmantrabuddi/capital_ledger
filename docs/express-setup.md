# Aurora Express setup

The demonstrated AWS deployment uses `DB_BACKEND=aurora_iam`. It connects to Aurora PostgreSQL Express over verified TLS using a fresh temporary IAM authentication token for each connection. No database password or Secrets Manager credential is needed for this connector.

The newer `infra/express.yaml` and `scripts/deploy-express.py` provision a new application around an **existing external Express cluster**, including the private recovery support in `infra/operations.yaml`. The complete new Express stack has been validated but has not been deployed from scratch. The older `infra/template.yaml` instead provisions a full-configuration Aurora cluster and uses Data API; it is a different path, and `scripts/deploy.ps1` retains its Free-account guard.

## Express automation prerequisites

Create or select the Express cluster first. It must belong to the intended account and region, be available on port 5432, have IAM database authentication and its Express internet access gateway enabled, and support auto-pause with `MinCapacity=0`. The selected PostgreSQL user and logical database must exist and permit application schema initialization. The script checks these AWS configuration properties without changing database settings or upgrading the account plan.

Use a current AWS CLI v2 profile, Python and pip. Start with this read-only plan from the repository root:

```powershell
python scripts/deploy-express.py --expected-account YOUR_12_DIGIT_ACCOUNT_ID --region us-east-2 --profile ledger-dev --cluster-identifier YOUR_EXISTING_EXPRESS_CLUSTER
```

The script requires a verified active Free plan with available USD credits. `--apply` explicitly creates or updates its separately owned application stack; the default `capital-ledger-express` prefix is distinct from the existing manual app. It packages the native runtime, references the existing Aurora endpoint/resource ID, bootstraps the selected logical database, and generates the frontend configuration from stack outputs. It does not create Aurora, SQL users, or logical databases. Use `--database-user` and `--database-name` when these differ from `postgres`. See [deployment](deployment.md#new-express-application-automation) for all options and the separate existing-app recovery path.

## Lambda configuration

The demonstrated runtime is **Python 3.12 on ARM64**, with **256 MB memory** and a **30-second timeout**. Use these handlers:

- Customer function: `ledger.handlers.accounts_handler`
- Transaction function: `ledger.handlers.transactions_handler`

Both functions need the following environment variables:

```text
DB_BACKEND=aurora_iam
AUTH_MODE=cognito
AURORA_HOST=<your Express cluster endpoint>
AURORA_PORT=5432
AURORA_USERNAME=postgres
AURORA_DATABASE=postgres
AURORA_CONNECT_TIMEOUT=10
ACCOUNTS_TABLE=<your customer table>
OUTBOX_QUEUE_URL=<the recovery support stack's OutboxQueueUrl>
```

The queue setting is required when enabling automatic private recovery; leaving it unset means the HTTP functions cannot signal that worker. The cluster identifier and logical SQL database name are different values. Express initially provides the `postgres` logical database. Choose a region permitted by the account; the demonstrated deployment uses `us-east-2`.

## Package the native driver

The Express deployment script packages and checks the dependencies automatically. For a manual function update, use a clean `.build/lambda` directory. The following PowerShell example downloads Linux ARM64 wheels even when preparing the package on Windows:

```powershell
New-Item -ItemType Directory -Force .build/lambda | Out-Null
python -m pip install --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 --only-binary=:all: --target .build/lambda "psycopg[binary]==3.3.6" "typing_extensions>=4.6"
Copy-Item -Recurse backend/ledger .build/lambda/ledger
Compress-Archive -Path .build/lambda/* -DestinationPath .build/lambda.zip -Force
```

The ZIP root must contain `ledger/`, the driver packages and their adjacent native libraries. Keep `ledger/aws-trust-ca.pem` in place: it contains public Amazon root certificates used for hostname- and certificate-checked TLS. `typing_extensions` is required by this driver on Python 3.12. Do not upload the ARM64 package to an x86_64 function or a different Python runtime.

## Permissions and initialization

The execution role must trust `lambda.amazonaws.com` and allow `rds-db:connect` on:

```text
arn:aws:rds-db:<region>:<account-id>:dbuser:<cluster-resource-id>/<database-user>
```

Also allow `dynamodb:GetItem` and `dynamodb:PutItem` on the customer table, `sqs:SendMessage` on the recovery queue ARN when configured, and the scoped CloudWatch Logs actions. The IAM resource ID is not the cluster's friendly identifier. The authenticated PostgreSQL user must have the SQL privileges needed by the schema and application.

Initialize the schema through an authorized IAM/TLS connection or a separately configured private `ledger.handlers.bootstrap_handler`. Do not publish bootstrap, worker, maintenance or reconciliation handlers as public HTTP routes.

## Private recovery and credit usage

`infra/operations.yaml` adds a private Python 3.12 ARM64 `ledger.handlers.maintenance_handler` with its own scoped role and a 120-second timeout. It receives SQS wake-up signals, uses a 30-second database connect timeout, drains at most 100 SQL outbox events, and emits count-only reconciliation logs. Only a full successful batch sends one continuation. Failures retry through SQS and eventually reach the source queue's dead-letter queue. Keep default AWS recursion protection enabled; see [outbox processing](deployment.md#outbox-processing) for large-backlog limits, queue settings, and safe investigation.

For the existing manual app, deploy only this support template and explicitly connect both updated HTTP functions to its queue URL and scoped send permission. The new Express application template includes that integration automatically. Neither route makes maintenance a public API endpoint.

The daily backstop is enabled by default with `EnableDailyRecovery=true`. It can briefly wake a paused Aurora instance and consume credits. Use `EnableDailyRecovery=false` for the standalone support stack, or `--disable-daily-recovery` for the new Express stack, to omit the scheduled wake-up. Event-driven API signals still operate, but a lost notification then requires another mutation or an independent operator signal. Database transactions close their connections so the worker does not keep Aurora alive by continuous polling. Database, queue, Lambda, logging and storage usage can consume credits; these resources are not a promise of permanent zero cost.

## Verified deployment

The deployed Cognito/API Gateway flow, both Lambda handlers, Aurora IAM/TLS access, and DynamoDB synchronization were exercised on October 31, 2026. The fictional demonstration account opened with $100, received $25, withdrew $10, persisted at $115 after reload, and rejected a $200 withdrawal. Aurora stored the three successful transactions; DynamoDB matched 11500 cents at version 3. This historical workflow evidence does not establish successful queue-driven recovery or a complete fresh Express-stack deployment. See [validation](validation.md) for current test and live deployment evidence.
