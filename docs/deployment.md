# AWS deployment

Capital Ledger was deployed and demonstrated on October 31, 2026 using Aurora PostgreSQL Express with IAM authentication. The repository now includes an Express-compatible application template and an additive recovery support template. The older full-configuration SAM path remains separate. The complete new Express application stack has been validated but has not been deployed from scratch; [validation](validation.md) records live deployment evidence separately.

## Deployment paths

| Path | Database connection | Provisioning |
| --- | --- | --- |
| Existing manual Express deployment | `DB_BACKEND=aurora_iam`; direct PostgreSQL with temporary IAM tokens and verified TLS | Existing Aurora Express cluster and separately configured application resources; `infra/operations.yaml` can add private recovery without adopting them |
| New Express application stack | `DB_BACKEND=aurora_iam`; same IAM/TLS connector | `infra/express.yaml` through `scripts/deploy-express.py`; references an existing external Express cluster and nests `infra/operations.yaml` |
| Older full-configuration template | `DB_BACKEND=aurora`; RDS Data API with an RDS-managed secret | `infra/template.yaml` through `scripts/deploy.ps1`; creates its own full-configuration Aurora cluster |
| Local development | SQLite by default; optional explicit PostgreSQL connection | `python start-local.py` |

The older full-configuration template does not reproduce the manual Express deployment. Its PowerShell script stops on a Free account before provisioning; preserve that guard. The new Express Python script instead requires a verified active Free plan and available USD credits, and never upgrades the account. See [Express setup](express-setup.md) for connector prerequisites and packaging.

## Demonstrated resources

- **Frontend:** four static files in S3, with `index.html` configured as the website index. Block Public Access remains enabled. CloudFront serves the private S3 REST origin through origin access control, with HTTPS and `index.html` as its default root object. The direct S3 website endpoint is not the serving path.
- **Customer Lambda:** Python 3.12, ARM64, handler `ledger.handlers.accounts_handler`.
- **Transaction Lambda:** the same runtime and architecture, handler `ledger.handlers.transactions_handler`.
- **Lambda settings:** 256 MB memory and a 30-second timeout; the environment selects `aurora_iam`, `cognito`, the existing database endpoint, and the customer table.
- **Aurora:** PostgreSQL 17.9 Express, IAM database authentication, verified TLS, Serverless capacity from 0 to 4 ACUs, and a five-minute auto-pause setting.
- **DynamoDB:** String partition key `account_id`, storing versioned customer profiles and balance summaries.
- **API Gateway:** nine HTTP API routes; eight protected by the Cognito JWT authorizer and `ledger-api/access` scope. `GET /api/health` is public. CORS allows the exact frontend origin and the required authorization, content-type and idempotency-key headers.
- **Cognito:** public application client without a secret, authorization-code flow with PKCE, exact callback/logout URLs, and `openid`, `email`, `ledger-api/access` scopes.
- **IAM:** Lambda service trust; `rds-db:connect` restricted to the cluster resource ID and database user, DynamoDB GetItem/PutItem restricted to the customer table, and project log permissions.

Cognito authenticates browser users; IAM authorizes AWS resource access. Account ownership checks use the verified subject passed by API Gateway. No local development login is exposed in AWS.

## Outbox processing

Each SQL mutation commits the balance, ledger, saved idempotent response and outbox event together. A bounded post-commit attempt delivers the returned account snapshot to DynamoDB. Updated HTTP handlers also send a generic signal to `OUTBOX_QUEUE_URL` after a successful mutation. No account details or authoritative event payload travel through SQS. A notification failure or a near-deadline deferral preserves the committed HTTP result and durable SQL outbox.

`infra/operations.yaml` creates a private `ledger.handlers.maintenance_handler` Lambda, encrypted SQS source/dead-letter queues, the event-source mapping, a restricted execution role, 14-day logs, and a configurable daily EventBridge backstop. It references an existing Express endpoint, cluster resource ID, customer table, and Lambda ZIP. The new Express application template nests this support stack; the manual application can attach it separately.

Each invocation drains **at most 100 SQL events**, then runs reconciliation. A full successful batch sends one continuation signal; an empty or partial batch sends none. A delivery failure or financial mismatch raises an error, so the whole SQS signal batch retries. Duplicate signals and repeated deliveries are safe because snapshot versions cannot regress. Reconciliation logs include counts, rather than customer identifiers, profile data, or balances.

The SQS mapping accepts up to 10 wake-up messages per invocation and has maximum concurrency 2. This does not multiply the 100-event SQL limit. Maintenance has a 120-second Lambda timeout and a 30-second database connect timeout. The source queue has a 900-second visibility timeout, four-day retention, and a redrive policy with `maxReceiveCount: 4`; its dead-letter queue retains messages for 14 days. Investigate the failure before redriving a dead-letter signal.

**Keep AWS recursive-loop protection enabled.** The template preserves the default termination behavior. AWS can stop a continuation chain after approximately 16 linked invocations; blocked SQS messages continue toward the source queue's redrive limit. An unusually large backlog can therefore need an independent API/daily notification or operator review. Do not set `RecursiveLoop` to `Allow` to bypass this limit. See [AWS recursive-loop detection](https://docs.aws.amazon.com/lambda/latest/dg/invocation-recursion.html) and [SQS batch retry behavior](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html).

`EnableDailyRecovery=true` is the default. It sends one signal per day to cover a lost post-commit notification, including a process stopping after commit. Set it to `false`, or use `--disable-daily-recovery` with the Express deployment script, to disable the scheduled wake-up. With the backstop disabled, a lost notification needs another mutation or an explicit independent operator signal. No worker loop polls Aurora continuously, and connections close after transactions, but the daily check can briefly resume an otherwise paused database and consume credits. Queue requests, invocations, logs, retained storage, and database usage can also consume credits; an active Free plan is not a zero-usage guarantee.

### Attach recovery to the existing manual application

Deploy only `infra/operations.yaml` as a separate support stack when keeping the current application resources. Supply `ResourcePrefix`, `AuroraHost`, `AuroraClusterResourceId`, `AuroraUsername`, `AuroraDatabase`, `AccountsTableName`, `LambdaArtifactBucket`, `LambdaArtifactKey`, and the desired `EnableDailyRecovery`. The artifact must contain the current backend and Python 3.12 ARM64 dependencies. CloudFormation needs `CAPABILITY_NAMED_IAM` for its scoped maintenance role.

After the support stack is ready, update both existing HTTP Lambdas to the same backend package, add its `OutboxQueueUrl` output as `OUTBOX_QUEUE_URL` while preserving the other environment variables, and grant their execution role `sqs:SendMessage` on that stack's `OutboxQueueArn` only. The support template does not update or adopt those existing functions for you. Its other outputs identify the maintenance function and dead-letter queue. Keep maintenance, bootstrap, and reconciliation private; add no HTTP route for them.

Validate queue-triggered delivery, recipient snapshots after transfers, count-only reconciliation logs, and retries/dead-letter handling in the target account. Template validation and local handler tests do not establish that live recovery has succeeded.

## New Express application automation

`infra/express.yaml` provisions the application around an **already existing** Express cluster: private S3/CloudFront, Cognito, nine HTTP routes (eight JWT-protected and public health), DynamoDB, two HTTP Lambdas, private bootstrap, and the nested recovery support stack. It neither creates nor adopts an Aurora cluster. A database user, logical database, IAM authentication, Express internet access gateway, port 5432, and auto-pause support with `MinCapacity=0` must already be configured. The CLI also checks account, region, endpoint, cluster resource ID, and available status.

Use a dedicated logical database for a new application. This script is not a migration of an existing ledger: its new DynamoDB table does not automatically backfill snapshots whose outbox rows were already marked delivered to another table.

From the repository root, first inspect the read-only plan:

```powershell
python scripts/deploy-express.py --expected-account YOUR_12_DIGIT_ACCOUNT_ID --region us-east-2 --profile ledger-dev --cluster-identifier YOUR_EXISTING_EXPRESS_CLUSTER
```

The default stack and resource prefix are `capital-ledger-express`, distinct from the existing manual application's reserved `capital-ledger` prefix. Add `--apply` to the same command only when you intend to create the new resources or update a stack already owned by this script. It does not migrate the manual application's resources. Optional arguments include `--database-user`, `--database-name`, `--stack-name`, `--resource-prefix`, `--artifact-bucket`, `--cognito-domain-prefix`, `--development-origin http://localhost:8787`, and `--disable-daily-recovery`.

The apply path refreshes its guards, packages Linux Python 3.12 ARM64 dependencies, uploads the backend and nested support template, deploys CloudFormation, invokes private schema bootstrap in the selected logical database, and uploads the four frontend files with generated Cognito/API configuration. It does not create SQL users or logical databases. Outputs and generated configuration stay under ignored `.build/`. A fresh complete application deployment still needs an end-to-end validation run; the current evidence covers template, guard, and packaging validation rather than a new stack provisioned from scratch.

## Reproduce the manual Express configuration

1. Authenticate an AWS CLI profile and choose a region permitted by your account. Examples use `us-east-2`; restrictions observed in one account do not apply to every account.
2. Create or select an Aurora PostgreSQL Express cluster with IAM authentication. Initialize the project schema using an authorized connection or the private bootstrap handler.
3. Create the DynamoDB customer table with String partition key `account_id`.
4. Package the Python 3.12 Linux ARM64 driver and `backend/ledger` as described in [Express setup](express-setup.md). Create both Lambda functions with the handlers and environment above.
5. Create the scoped execution role and grant API Gateway permission to invoke the corresponding functions.
6. Configure Cognito and the HTTP API routes. Register the exact frontend callback, logout and CORS origins.
7. Use [the browser configuration example](../frontend/config.aws.example.js) to generate a deployment-only `config.js`. Upload it with `index.html`, `styles.css` and `app.js` to the private S3 bucket, then configure CloudFront.
8. Attach the private recovery support stack as described above, or explicitly retain manual replay responsibility.
9. Test sign-in, account creation, deposits, withdrawals, rejected overdrafts, transfers, recovery and persistence. Compare the actual Aurora transaction rows and DynamoDB summary for the same fictional account.

All account identifiers and endpoints belong in your deployment configuration. The tracked browser configuration defaults to the local application.

## Optional full-configuration SAM path

This path creates a VPC, a full-configuration Aurora cluster with an RDS-managed secret, API Gateway, Cognito, DynamoDB, S3/CloudFront, and private bootstrap/worker/reconciliation functions. It uses Data API rather than the Express IAM connector. It has not been used to provision the demonstrated deployment.

On an account that supports these resources, from the repository root:

```powershell
.\scripts\deploy.ps1 -Profile ledger-dev -Region us-east-2
```

Optional parameters include `-ArtifactBucket`, `-CognitoDomainPrefix`, `-StackName`, `-AuroraEngineVersion`, `-ExistingLambdaRoleArn`, and `-DisableSchedules`. The script verifies identity and account plan, validates the template, packages the backend, deploys with schedules initially disabled, bootstraps the schema, uploads generated frontend configuration, and enables schedules unless disabled. Generated artifacts remain under `.build/`.

The template has a 0.5 ACU minimum and 2 ACU maximum, so it does not provide zero idle compute cost. Review the generated resource plan before running it. The template's Data API roles and managed secret differ from the Express role described above.

## Resource lifecycle

Preserve the source and schema for further development. The demonstrated application resources were configured separately. An additive operations stack owns only its support resources, not the database or existing HTTP functions. Use the actual owning stack and identifiers when reviewing or removing resources.

The new Express application stack does not own the external Aurora cluster. Its DynamoDB table and frontend bucket are retained on deletion; its artifact bucket also remains. Deleting recovery queues discards their wake-up notifications, while committed ledger records and pending outbox rows remain in Aurora. Review outstanding work before removing recovery infrastructure.

For a stack created by the optional deploy script, stack deletion retains the DynamoDB table and frontend bucket and takes an Aurora final snapshot. The separately created artifact bucket also remains. Review retained storage, snapshots and secrets explicitly; deleting a stack is not equivalent to removing every resource.

## References

- [Aurora Express configuration](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_GettingStartedAurora.AuroraPostgreSQL.ExpressConfig.html)
- [Aurora Free Tier restrictions](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-free-tier.html)
- [IAM PostgreSQL connections](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/UsingWithRDS.IAMDBAuth.Connecting.AWSCLI.PostgreSQL.html)
- [CloudFront private S3 origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)
- [Cognito authorization endpoint and PKCE](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
