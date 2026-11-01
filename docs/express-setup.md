# Aurora Express setup

The demonstrated AWS deployment uses `DB_BACKEND=aurora_iam`. It connects to Aurora PostgreSQL Express over verified TLS using a fresh temporary IAM authentication token for each connection. No database password or Secrets Manager credential is needed for this connector.

The optional `infra/template.yaml` instead provisions a full-configuration Aurora cluster and uses Data API. It is not the Express deployment path. `scripts/deploy.ps1` retains its Free-account guard.

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
ACCOUNTS_TABLE=<your customer table>
```

The cluster identifier and logical SQL database name are different values. Express initially provides the `postgres` logical database. Choose a region permitted by the account; the demonstrated deployment uses `us-east-2`.

## Package the native driver

Use a clean `.build/lambda` directory. The following PowerShell example downloads Linux ARM64 wheels even when preparing the package on Windows:

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

Also allow `dynamodb:GetItem` and `dynamodb:PutItem` on the customer table and the scoped CloudWatch Logs actions. The IAM resource ID is not the cluster's friendly identifier. The authenticated PostgreSQL user must have the SQL privileges needed by the schema and application.

Initialize the schema through an authorized IAM/TLS connection or a separately configured private `ledger.handlers.bootstrap_handler`. Do not publish bootstrap, worker or reconciliation handlers as public HTTP routes. For the demonstrated two-function deployment, outbox processing uses a bounded post-commit attempt; scheduled retry and reconciliation were not deployed. See [deployment details](deployment.md).

## Verified deployment

The deployed Cognito/API Gateway flow, both Lambda handlers, Aurora IAM/TLS access, and DynamoDB synchronization were exercised on October 31, 2026. The fictional demonstration account opened with $100, received $25, withdrew $10, persisted at $115 after reload, and rejected a $200 withdrawal. Aurora stored the three successful transactions; DynamoDB matched 11500 cents at version 3. See [validation](validation.md) for test and deployment evidence.
