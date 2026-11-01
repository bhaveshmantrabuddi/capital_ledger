# Local quickstart

Requires Python 3.11 or newer. The local app needs no AWS account, API keys, Docker, or package installation.

## Start

From the repository root:

```powershell
python start-local.py
```

On macOS/Linux, use `python3 start-local.py`. Windows users can also double-click `start-local.cmd` and keep its terminal window open.

Open **http://127.0.0.1:8787** and select Alice or Bob. All identities and balances are fictional.

If the port is occupied, run `python start-local.py --port 8788` and open `http://127.0.0.1:8788`. Press Ctrl+C in the terminal to stop the server.

## Try the workflow

1. Create an account with fictional profile details and a $100 starting balance.
2. Deposit $25, then withdraw $10. The balance becomes $115.
3. Attempt a withdrawal larger than the balance and confirm it is rejected.
4. Refresh the page to confirm persistence.
5. Pause summary synchronization, perform an operation, then resume delivery to observe recovery.
6. Inspect reconciliation and switch between the two local identities to test ownership boundaries.

If an operation has an uncertain outcome, use the saved retry action. It reuses the original idempotency key.

## Local and cloud modes

The tracked frontend configuration uses the local API. Data stays in `.local/ledger.sqlite3`, outside Git. `DATABASE_URL` cannot redirect the default starter; PostgreSQL requires an explicit `--postgres` option.

The hosted AWS version uses Cognito authentication and separate customer and transaction Lambdas. It does not expose local identity switching or worker controls. See [README.md](README.md) and [Express setup](docs/express-setup.md) for the cloud path.
