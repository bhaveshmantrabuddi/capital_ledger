CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    balance_cents BIGINT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    version BIGINT NOT NULL DEFAULT 1,
    system_account INTEGER NOT NULL DEFAULT 0 CHECK (system_account IN (0,1)),
    created_at TEXT NOT NULL,
    CHECK (balance_cents >= 0 OR system_account = 1)
);
CREATE INDEX IF NOT EXISTS accounts_owner_idx ON accounts(owner_id, created_at);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('opening','deposit','withdrawal','transfer','reversal')),
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    owner_id TEXT NOT NULL,
    source_account_id TEXT NOT NULL REFERENCES accounts(id),
    destination_account_id TEXT NOT NULL REFERENCES accounts(id),
    description TEXT NOT NULL,
    reverses_id TEXT UNIQUE REFERENCES transactions(id),
    created_at TEXT NOT NULL,
    CHECK (source_account_id <> destination_account_id)
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_cents BIGINT NOT NULL CHECK (amount_cents <> 0),
    created_at TEXT NOT NULL,
    UNIQUE(transaction_id, account_id)
);
CREATE INDEX IF NOT EXISTS entries_account_idx ON ledger_entries(account_id, created_at);
CREATE TABLE IF NOT EXISTS idempotency_requests (
    owner_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_id, request_key)
);
CREATE TABLE IF NOT EXISTS outbox_events (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    version BIGINT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts BIGINT NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE(account_id, version)
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox_events(delivered_at, created_at);
CREATE TABLE IF NOT EXISTS summary_accounts (
    account_id TEXT PRIMARY KEY,
    version BIGINT NOT NULL,
    payload TEXT NOT NULL
);
