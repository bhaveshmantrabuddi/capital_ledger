# Midterm requirements and implementation evidence

Capital Ledger is a simulated banking application built for the Capital One option in CMSC 398P. It is an independent student project and is not affiliated with, endorsed by, or connected to Capital One. Accounts, identities, dates of birth, balances, and transactions used in the demo are fictional. It does not connect to a bank or move real money.

The course's written requirements and company diagram define the minimum. The diagram is an architecture sketch; the written requirement to update the DynamoDB balance still applies even where an arrow is omitted.

| Required behavior | Implementation | How to demonstrate it |
|---|---|---|
| S3 static website hosting | Static HTML, CSS, and JavaScript frontend; S3 `WebsiteConfiguration` is enabled. Functional HTTPS delivery uses CloudFront with a private S3 REST origin. | Open the deployed CloudFront website and show S3 assets and the enabled static website setting. The S3 website endpoint remains private/inaccessible; it is not the serving path. This distinction is documented in `docs/deployment.md` for any instructor-specific interpretation of the requirement. |
| Create a customer with full name, date of birth, and starting balance | Account creation form and `POST /api/accounts`; customer Lambda validates the profile and creates an opening-balance ledger entry. | Create a fictional customer, then show the resulting profile and starting balance. |
| Customer Lambda stores and retrieves customer information in DynamoDB | Customer handler plus a versioned DynamoDB account snapshot containing the profile and balance. | Show the customer Lambda and a DynamoDB item for the account; retrieve the same account in the UI. |
| Separate transaction Lambda handles deposits and withdrawals | Transaction handler exposes dedicated deposit and withdrawal routes. | Deposit and withdraw through the UI and inspect the transaction Lambda and its logs. |
| Deposit/withdrawal updates the balance in DynamoDB | Committed SQL operations create durable outbox events. A post-commit attempt updates the DynamoDB snapshot. The November 1 extension adds deployed private SQS recovery and a daily backstop for missed notifications. | Wait for synchronization, then show the new DynamoDB balance. See the live delayed-delivery recovery evidence in `docs/validation.md`. |
| Transactions recorded in Aurora | PostgreSQL ledger, transaction records, balances, idempotency records, and outbox events commit in one SQL transaction. The demonstrated AWS deployment uses Aurora PostgreSQL Express through IAM-authenticated, certificate-checked PostgreSQL connections. | Query Aurora and show the transaction and associated ledger entries. A local PostgreSQL result is useful development evidence but does not prove Aurora deployment. |
| IAM controls; guide wording is “IAM managing user authentication” | Function execution roles and resource permissions in infrastructure configuration; Cognito and the API Gateway JWT authorizer authenticate browser customers. These are distinct responsibilities. | Show IAM policies scoped to the intended resources, sign-in, and account ownership checks. If the instructor specifically requires IAM-signed browser requests, Cognito JWT sign-in alone is not that mechanism; follow the deployment notes for the supported authentication design. |
| Narrated demonstration of functionality, relevant code, and AWS configuration | Recording outline in `docs/demo-script.md`. | Record the working deployed application and AWS console/configuration evidence. A narrated recording of the deployed application and actual console configuration was completed on October 31, 2026; this repository keeps the reusable outline. |

## Extensions that add backend depth

- Append-only ledger movements and matching entries for each transfer.
- Atomic account updates and ownership checks against the authoritative SQL store.
- Database-enforced idempotency so a retried successful operation returns its original result.
- Versioned DynamoDB snapshots and durable outbox recovery.
- Private SQS maintenance, retry/dead-letter handling, and a configurable daily backstop.
- Reconciliation that detects discrepancies instead of silently changing financial state.
- Automated HTTP, concurrency, and failure-recovery tests; reproducible benchmark tooling.

The SQL ledger is the authority for decisions about available funds. DynamoDB retains the required customer profile and a balance summary that catches up after a delivery outage. This is eventual consistency across the stores, not an atomic transaction spanning Aurora and DynamoDB.

## Completion boundary

Live deployment evidence is recorded in [validation](validation.md), separately from local tests. The October 31 demonstration verified real AWS resources, browser authentication, database records and the required account workflow. The November 1 extension verified actual SQS-triggered recovery and count-only reconciliation. Local mocks and PostgreSQL benchmarks remain local evidence; a complete fresh Express application stack has not been provisioned from scratch.

Use only fictional profile information in the public repository and video. The guide says the final project builds on the midterm: preserve the source, migrations, setup steps, and needed test data; manage running AWS resources as described in the deployment notes rather than discarding the work.
