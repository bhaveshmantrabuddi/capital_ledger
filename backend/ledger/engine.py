"""Financial rules shared by the local server and the AWS Lambda handlers."""
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import re
import time
import uuid

LOG = logging.getLogger(__name__)
MAX_AMOUNT = 100_000_000_00  # Simulator limit: $100 million per account/operation.
FUNDING = 'system-funding'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class LedgerError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def amount(value, zero=False):
    if type(value) is not int or value < (0 if zero else 1) or value > MAX_AMOUNT:
        raise LedgerError('invalid_amount', 'Amount must be integer cents within the simulator limit.')
    return value


def text(value, field, maximum=120):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise LedgerError('invalid_input', f'{field} must contain 1 to {maximum} characters.')
    return value.strip()


class Ledger:
    def __init__(self, database, projection):
        self.db = database
        self.projection = projection
        self.worker_paused = False
        # Tests can interrupt a named boundary. Never exposed through HTTP/AWS.
        self.fault_hook = None

    def fault(self, point):
        if self.fault_hook:
            self.fault_hook(point)

    def _idempotent(self, owner, key, operation, payload, perform):
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:\-]{8,128}', key):
            raise LedgerError('idempotency_required', 'Provide an Idempotency-Key of 8 to 128 safe characters.')
        fingerprint = hashlib.sha256(json.dumps(
            {'operation': operation, 'payload': payload}, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        for attempt in range(4):
            try:
                with self.db.transaction() as tx:
                    tx.execute('''INSERT INTO idempotency_requests
                        (owner_id,request_key,fingerprint,created_at)
                        VALUES (:owner,:key,:fingerprint,:created)
                        ON CONFLICT (owner_id,request_key) DO NOTHING''',
                        dict(owner=owner, key=key, fingerprint=fingerprint, created=now()))
                    record = tx.one(tx.lock('''SELECT fingerprint,response_json
                        FROM idempotency_requests WHERE owner_id=:owner AND request_key=:key'''),
                        dict(owner=owner, key=key))
                    if record['fingerprint'] != fingerprint:
                        raise LedgerError('idempotency_conflict',
                            'This key was already used for a different request.', 409)
                    if record['response_json']:
                        result = json.loads(record['response_json'])
                    else:
                        result = perform(tx)
                        tx.execute('''UPDATE idempotency_requests SET response_json=:response
                            WHERE owner_id=:owner AND request_key=:key''',
                            dict(response=json.dumps(result, sort_keys=True), owner=owner, key=key))
                        self.fault('before_commit')
                break
            except Exception as exc:
                if not self.db.retryable(exc) or attempt == 3:
                    raise
                time.sleep(0.02 * (attempt + 1))
        self.fault('after_commit')
        if not self.worker_paused:
            try:
                # Only fast-forward the returned account. The scheduled worker owns
                # the backlog and the recipient's eventual summary after a transfer.
                account_id = result.get('account', {}).get('id')
                if account_id:
                    self.drain_outbox(limit=1, account_id=account_id, newest_first=True)
            except Exception:
                # A committed financial operation must remain successful if its projection is down.
                LOG.exception('projection_fast_path_failed')
        result = json.loads(json.dumps(result))
        account = result.get('account')
        if account:
            result['projection_synced'] = self._projection_matches(account)
        return result

    def _projection_matches(self, account):
        try:
            summary = self.projection.get(account['id'])
            if not summary:
                return False
            if summary['version'] == account['version']:
                return summary['balance_cents'] == account['balance_cents']
            if summary['version'] > account['version']:
                with self.db.transaction(False) as tx:
                    current = self._account(tx, account['id'])
                return (summary['version'] == current['version'] and
                        summary['balance_cents'] == current['balance_cents'])
            return False
        except Exception:
            LOG.warning('projection_read_unavailable', exc_info=True)
            return False

    def _account(self, tx, account_id, owner=None, lock=False):
        sql = 'SELECT * FROM accounts WHERE id=:id AND system_account=0'
        row = tx.one(tx.lock(sql) if lock else sql, {'id': account_id})
        if not row:
            raise LedgerError('account_not_found', 'Account not found.', 404)
        if owner is not None and row['owner_id'] != owner:
            raise LedgerError('forbidden', 'This account belongs to another user.', 403)
        return row

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ('id','owner_id','name','full_name',
            'date_of_birth','balance_cents','currency','version','created_at')}

    def _enqueue(self, tx, row):
        snapshot = self._public(row)
        tx.execute('''INSERT INTO outbox_events
            (id,account_id,version,payload,created_at)
            VALUES (:id,:account,:version,:payload,:created)''',
            dict(id=str(uuid.uuid4()), account=row['id'], version=row['version'],
                 payload=json.dumps(snapshot, sort_keys=True), created=now()))

    def _record(self, tx, owner, kind, value, source, destination, description):
        transaction_id, timestamp = str(uuid.uuid4()), now()
        tx.execute('''INSERT INTO transactions
            (id,kind,amount_cents,owner_id,source_account_id,destination_account_id,description,created_at)
            VALUES (:id,:kind,:amount,:owner,:source,:destination,:description,:created)''',
            dict(id=transaction_id, kind=kind, amount=value, owner=owner,
                 source=source, destination=destination, description=description, created=timestamp))
        for account_id, signed in ((source, -value), (destination, value)):
            tx.execute('''INSERT INTO ledger_entries
                (id,transaction_id,account_id,amount_cents,created_at)
                VALUES (:id,:transaction,:account,:amount,:created)''',
                dict(id=str(uuid.uuid4()), transaction=transaction_id,
                     account=account_id, amount=signed, created=timestamp))
        return transaction_id

    def create_account(self, owner, key, payload):
        name = text(payload.get('name'), 'Account name', 60)
        full_name = text(payload.get('full_name'), 'Full name')
        dob = payload.get('date_of_birth')
        try:
            birthday = date.fromisoformat(dob)
            if birthday > date.today() or birthday < date(1900, 1, 1):
                raise ValueError()
        except (TypeError, ValueError):
            raise LedgerError('invalid_input', 'Use a valid birth date in YYYY-MM-DD format.')
        initial = amount(payload.get('initial_balance_cents', 0), zero=True)
        canonical = dict(name=name, full_name=full_name, date_of_birth=dob,
                         initial_balance_cents=initial)

        def perform(tx):
            account_id, created = str(uuid.uuid4()), now()
            tx.execute('''INSERT INTO accounts
                (id,owner_id,name,full_name,date_of_birth,balance_cents,currency,version,created_at)
                VALUES (:id,:owner,:name,:full_name,:dob,:balance,'USD',1,:created)''',
                dict(id=account_id, owner=owner, name=name, full_name=full_name,
                     dob=dob, balance=initial, created=created))
            transaction_id = None
            if initial:
                tx.execute('UPDATE accounts SET balance_cents=balance_cents-:amount WHERE id=:id',
                           dict(amount=initial, id=FUNDING))
                transaction_id = self._record(tx, owner, 'opening', initial,
                    FUNDING, account_id, 'Simulated opening funds')
            account = self._account(tx, account_id)
            self._enqueue(tx, account)
            return dict(account=self._public(account), transaction_id=transaction_id)
        return self._idempotent(owner, key, 'create_account', canonical, perform)

    def _movement(self, owner, key, kind, source, destination, payload):
        value = amount(payload.get('amount_cents'))
        description = payload.get('description', '')
        if not isinstance(description, str) or len(description) > 240:
            raise LedgerError('invalid_input', 'Description must be at most 240 characters.')
        if source == destination:
            raise LedgerError('invalid_transfer', 'Choose two different accounts.')
        if not isinstance(source, str) or not isinstance(destination, str):
            raise LedgerError('invalid_input', 'Account IDs are required.')
        canonical = dict(source=source, destination=destination, amount_cents=value,
                         description=description.strip())

        def perform(tx):
            rows = {}
            # A global account order prevents opposing transfers from locking in reverse order.
            for account_id in sorted((source, destination)):
                row = tx.one(tx.lock('SELECT * FROM accounts WHERE id=:id'), {'id': account_id})
                if not row:
                    raise LedgerError('account_not_found', 'Account not found.', 404)
                rows[account_id] = row
            controlled = rows[destination if kind == 'deposit' else source]
            if controlled['owner_id'] != owner or controlled['system_account']:
                raise LedgerError('forbidden', 'This account belongs to another user.', 403)
            if kind == 'transfer' and (rows[source]['system_account'] or rows[destination]['system_account']):
                raise LedgerError('invalid_transfer', 'System accounts cannot be transfer targets.')
            if not rows[source]['system_account'] and rows[source]['balance_cents'] < value:
                raise LedgerError('insufficient_funds', 'Insufficient available funds.', 409)
            if not rows[destination]['system_account'] and rows[destination]['balance_cents'] + value > MAX_AMOUNT:
                raise LedgerError('balance_limit', 'The destination would exceed the simulator balance limit.', 409)
            for account_id, delta in ((source, -value), (destination, value)):
                tx.execute('''UPDATE accounts SET balance_cents=balance_cents+:delta,
                    version=version+1 WHERE id=:id''', dict(delta=delta, id=account_id))
            transaction_id = self._record(tx, owner, kind, value, source, destination, description.strip())
            result_account = None
            for account_id in (source, destination):
                if not rows[account_id]['system_account']:
                    row = self._account(tx, account_id)
                    self._enqueue(tx, row)
                    if row['id'] == controlled['id']:
                        result_account = self._public(row)
            return dict(transaction_id=transaction_id, account=result_account)
        return self._idempotent(owner, key, kind, canonical, perform)

    def deposit(self, owner, account_id, key, payload):
        return self._movement(owner, key, 'deposit', FUNDING, account_id, payload)

    def withdraw(self, owner, account_id, key, payload):
        return self._movement(owner, key, 'withdrawal', account_id, FUNDING, payload)

    def transfer(self, owner, key, payload):
        return self._movement(owner, key, 'transfer', payload.get('source_account_id'),
                              payload.get('destination_account_id'), payload)

    def get_account(self, owner, account_id):
        with self.db.transaction(False) as tx:
            account = self._public(self._account(tx, account_id, owner))
        try:
            summary = self.projection.get(account_id)
        except Exception:
            summary = None
            LOG.warning('customer_summary_unavailable', exc_info=True)
        account.update(summary_balance_cents=summary['balance_cents'] if summary else None,
                       summary_version=summary['version'] if summary else None,
                       summary_synced=bool(summary and summary['version'] == account['version']
                           and summary['balance_cents'] == account['balance_cents']))
        return account

    def list_accounts(self, owner):
        with self.db.transaction(False) as tx:
            rows = tx.execute('''SELECT id FROM accounts WHERE owner_id=:owner
                AND system_account=0 ORDER BY created_at,id''', {'owner': owner})
        return [self.get_account(owner, row['id']) for row in rows]

    def list_transactions(self, owner, account_id=None):
        with self.db.transaction(False) as tx:
            if account_id:
                self._account(tx, account_id, owner)
                rows = tx.execute('''SELECT t.* FROM transactions t
                    JOIN ledger_entries e ON e.transaction_id=t.id
                    WHERE e.account_id=:account ORDER BY t.created_at DESC,t.id DESC LIMIT 200''',
                    {'account': account_id})
            else:
                rows = tx.execute('''SELECT DISTINCT t.* FROM transactions t
                    JOIN ledger_entries e ON e.transaction_id=t.id
                    JOIN accounts a ON a.id=e.account_id
                    WHERE a.owner_id=:owner AND a.system_account=0
                    ORDER BY t.created_at DESC,t.id DESC LIMIT 200''', {'owner': owner})
        # Do not disclose the initiating user's identity when viewing an incoming transfer.
        return [{k: v for k, v in r.items() if k not in ('owner_id','reverses_id')} for r in rows]

    def drain_outbox(self, limit=100, account_id=None, newest_first=False):
        if self.worker_paused:
            return {'processed': 0, 'failed': 0, 'paused': True}
        with self.db.transaction(False) as tx:
            sql = 'SELECT * FROM outbox_events WHERE delivered_at IS NULL'
            params = {'limit': max(1, min(int(limit), 500))}
            if account_id:
                sql += ' AND account_id=:account'
                params['account'] = account_id
            sql += (' ORDER BY created_at DESC,id DESC' if newest_first else ' ORDER BY created_at,id')
            events = tx.execute(sql + ' LIMIT :limit', params)
        processed = failed = 0
        for event in events:
            try:
                self.projection.put(json.loads(event['payload']))
                self.fault('after_projection_write')
                with self.db.transaction() as tx:
                    tx.execute('''UPDATE outbox_events SET delivered_at=:delivered,
                        attempts=attempts+1,last_error=NULL WHERE id=:id AND delivered_at IS NULL''',
                        {'id': event['id'], 'delivered': now()})
                processed += 1
            except Exception as exc:
                failed += 1
                with self.db.transaction() as tx:
                    tx.execute('''UPDATE outbox_events SET attempts=attempts+1,last_error=:error
                        WHERE id=:id AND delivered_at IS NULL''',
                        {'id': event['id'], 'error': type(exc).__name__})
                LOG.warning('outbox_delivery_failed', extra={'event_id': event['id']})
        return dict(processed=processed, failed=failed, paused=False)

    def reconcile(self, owner=None):
        with self.db.transaction(False) as tx:
            rows = tx.execute('''SELECT a.id,a.owner_id,a.balance_cents,a.version,a.system_account,
                CAST(COALESCE(SUM(e.amount_cents),0) AS BIGINT) AS ledger_balance
                FROM accounts a LEFT JOIN ledger_entries e ON e.account_id=a.id
                GROUP BY a.id,a.owner_id,a.balance_cents,a.version,a.system_account
                ORDER BY a.id''')
            unbalanced = tx.execute('''SELECT transaction_id,CAST(SUM(amount_cents) AS BIGINT) AS total,
                COUNT(*) AS entry_count FROM ledger_entries GROUP BY transaction_id
                HAVING SUM(amount_cents) <> 0 OR COUNT(*) <> 2''')
            pending = tx.one('''SELECT COUNT(*) AS count,MIN(created_at) AS oldest
                FROM outbox_events WHERE delivered_at IS NULL''')
        mismatches, lags, checked = [], [], 0
        for row in rows:
            if owner is not None and row['owner_id'] != owner:
                continue
            checked += 1
            if row['balance_cents'] != row['ledger_balance']:
                mismatches.append(dict(account_id=row['id'], kind='ledger_balance',
                    stored=row['balance_cents'], expected=row['ledger_balance']))
            if row['system_account']:
                continue
            try:
                summary = self.projection.get(row['id'])
            except Exception:
                summary = None
            if not summary or summary['version'] < row['version']:
                lags.append({'account_id': row['id'], 'expected_version': row['version'],
                             'summary_version': summary['version'] if summary else None})
            elif summary['version'] == row['version'] and summary['balance_cents'] != row['balance_cents']:
                mismatches.append(dict(account_id=row['id'], kind='summary_balance',
                    stored=summary['balance_cents'], expected=row['balance_cents']))
            elif summary['version'] > row['version']:
                # A writer may have committed after the consistent ledger snapshot.
                # Re-read before classifying an impossible future projection.
                with self.db.transaction(False) as tx:
                    current = self._account(tx, row['id'])
                if summary['version'] > current['version']:
                    mismatches.append(dict(account_id=row['id'], kind='summary_ahead',
                        stored=summary['version'], expected=current['version']))
                elif (summary['version'] == current['version'] and
                      summary['balance_cents'] != current['balance_cents']):
                    mismatches.append(dict(account_id=row['id'], kind='summary_balance',
                        stored=summary['balance_cents'], expected=current['balance_cents']))
        if owner is None:
            mismatches.extend(dict(kind='unbalanced_transaction', **r) for r in unbalanced)
        oldest_seconds = max(0, (datetime.now(timezone.utc) -
            datetime.fromisoformat(pending['oldest'])).total_seconds()) if pending['oldest'] else 0
        return dict(ok=not mismatches, accounts_checked=checked, mismatches=mismatches,
                    projection_lag=lags, pending_events=pending['count'],
                    oldest_pending_seconds=round(oldest_seconds, 3))
