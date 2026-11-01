# AWS deployment

Capital Ledger was deployed and demonstrated on October 31, 2026 using Aurora PostgreSQL Express with IAM authentication. The repository also retains a separate full-configuration SAM deployment path. These paths have different database connections and resource requirements.

## Deployment paths

| Path | Database connection | Provisioning |
| --- | --- | --- |
| Demonstrated deployment | `DB_BACKEND=aurora_iam`; direct PostgreSQL with temporary IAM tokens and verified TLS | Existing Aurora Express cluster, two Lambda functions, DynamoDB, API Gateway, Cognito, S3 and CloudFront; configured separately |
| Optional full-configuration template | `DB_BACKEND=aurora`; RDS Data API with an RDS-managed secret | `infra/template.yaml` through `scripts/deploy.ps1` |
| Local development | SQLite by default; optional explicit PostgreSQL connection | `python start-local.py` |

The current Express deployment is not reproduced by the full-configuration template. The deploy script checks the AWS account plan and stops on a Free account before provisioning. Preserve that guard. See [Express setup](express-setup.md) for the demonstrated connection path.

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

Each SQL mutation commits the balance, ledger, saved idempotent response and outbox event together. A bounded post-commit attempt delivers the returned account snapshot to DynamoDB. The demonstrated deployment has two HTTP Lambdas; it does not have an EventBridge outbox or reconciliation schedule.

The worker and reconciliation handlers are available for an optional private operational deployment. A pending snapshot requires a worker invocation or other explicit replay process; durable storage alone does not schedule retries. Transfers can leave the recipient snapshot pending until replay. The local server includes a background worker, and the alternate SAM template includes optional scheduled workers.

## Reproduce the Express configuration

1. Authenticate an AWS CLI profile and choose a region permitted by your account. Examples use `us-east-2`; restrictions observed in one account do not apply to every account.
2. Create or select an Aurora PostgreSQL Express cluster with IAM authentication. Initialize the project schema using an authorized connection or the private bootstrap handler.
3. Create the DynamoDB customer table with String partition key `account_id`.
4. Package the Python 3.12 Linux ARM64 driver and `backend/ledger` as described in [Express setup](express-setup.md). Create both Lambda functions with the handlers and environment above.
5. Create the scoped execution role and grant API Gateway permission to invoke the corresponding functions.
6. Configure Cognito and the HTTP API routes. Register the exact frontend callback, logout and CORS origins.
7. Use [the browser configuration example](../frontend/config.aws.example.js) to generate a deployment-only `config.js`. Upload it with `index.html`, `styles.css` and `app.js` to the private S3 bucket, then configure CloudFront.
8. Test sign-in, account creation, deposits, withdrawals, rejected overdrafts, and persistence. Compare the actual Aurora transaction rows and DynamoDB summary for the same fictional account.

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

Preserve the source and schema for further development. The demonstrated resources were configured separately and are not owned by the optional CloudFormation stack. Use their actual identifiers when reviewing or removing them.

For a stack created by the optional deploy script, stack deletion retains the DynamoDB table and frontend bucket and takes an Aurora final snapshot. The separately created artifact bucket also remains. Review retained storage, snapshots and secrets explicitly; deleting a stack is not equivalent to removing every resource.

## References

- [Aurora Express configuration](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_GettingStartedAurora.AuroraPostgreSQL.ExpressConfig.html)
- [Aurora Free Tier restrictions](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-free-tier.html)
- [IAM PostgreSQL connections](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/UsingWithRDS.IAMDBAuth.Connecting.AWSCLI.PostgreSQL.html)
- [CloudFront private S3 origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)
- [Cognito authorization endpoint and PKCE](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
