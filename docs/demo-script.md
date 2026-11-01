# Narrated demonstration script

Use fictional users and money throughout. The local demonstration proves local implementation behavior. To satisfy the AWS hosting and configuration requirements, record the deployed AWS application and resources as well. Do not present the local development login as Cognito sign-in or a local database as Aurora.

## Preparation

1. Run the automated tests and save their actual output.
2. Start the application using the README instructions, or deploy using `docs/deployment.md`.
3. Prepare two fictional customer identities and an empty test account. Avoid showing secrets, tokens, AWS access keys, or actual personal data while recording.
4. Open the frontend, the relevant source files, and database views. For the AWS segment, open S3, Lambda, DynamoDB, Aurora, and IAM configuration.

## 1. Explain the application

Say: “This is Capital Ledger, an independent CMSC 398P banking simulator. It uses fictional accounts and money and has no affiliation with Capital One. The required application creates customers, displays their balances, accepts deposits and withdrawals, and stores transaction history.”

Show the architecture diagram and identify the website, customer Lambda, transaction Lambda, DynamoDB customer snapshots, Aurora ledger, and IAM execution permissions. Briefly explain that Aurora authorizes balance changes and DynamoDB stores a synchronized customer balance.

## 2. Demonstrate the required customer workflow

1. Sign in as the first fictional customer.
2. Create an account with a fictional full name, a plausible fictional date of birth, and a starting balance of $100.
3. Show the new customer profile and $100 balance.
4. Deposit $25 and show $125.
5. Withdraw $10 and show $115.
6. Show the transaction history with the opening balance, deposit, and withdrawal.
7. Attempt an excessive withdrawal; show the rejected request and unchanged balance.

For AWS evidence, show the DynamoDB customer item with the final synchronized balance, the Aurora transaction and ledger rows, and the separate Lambda handlers responsible for customer and financial operations. Show the S3 frontend assets and IAM role policies.

## 3. Demonstrate additional backend behavior

1. **Transfers:** transfer $10 between owned test accounts. Show both balances changing and two opposite ledger movements under one transaction.
2. **Authorization:** switch to the second user and attempt to retrieve the first user's account. Show access denied.
3. **Safe retries:** replay the same deposit request with the same `Idempotency-Key`; show the same transaction ID and only one deposit. Change the amount while retaining the key and show the conflict.
4. **Concurrency:** run the automated concurrent withdrawal test against the documented backend. Show ten successful $10 withdrawals from $100, no negative balance, and the remaining requests rejected. Identify the actual database used.
5. **Recovery:** in local mode, pause snapshot delivery, perform an operation, and show its pending synchronization state. Resume delivery and show the snapshot catches up. Local debug controls are not part of the deployed public API.
6. **Reconciliation:** show the report comparing ledger-derived balances with stored balances and snapshots. Explain how a detected discrepancy would be investigated.

Do not claim a live AWS outage test from a local paused-worker demonstration. If an AWS recovery test was actually performed, describe the specific service interruption and observed result separately.

## 4. Explain the important code

Show the financial transaction boundary, stable account lock order, idempotency uniqueness, and outbox insertion. Then show version-conditional snapshot delivery. Explain the two boundaries:

- SQL balance, ledger, idempotency result, and outbox event commit together.
- DynamoDB delivery happens afterward and can be retried, so cross-store consistency is eventual.

Show one meaningful regression test for competing writes or a lost response. Describe the invariant it checks rather than narrating every line.

## 5. State measured results and deployment status

Use only the numbers in your own saved benchmark output. State the operation count, concurrency, backend, environment, throughput, and p95 latency. Separate expected insufficient-funds rejections from unexpected failures.

Close by identifying what was actually demonstrated: local behavior, PostgreSQL tests, AWS deployment, and/or recorded configuration evidence. Mark any remaining AWS verification explicitly. A narrated recording of the deployed application and console configuration was completed on October 31, 2026. This script remains a reusable outline for future demonstrations.
