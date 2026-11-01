"""Behavioral invariants, exercised against SQLite and optionally real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from ledger.database import Database
from ledger.engine import Ledger, LedgerError
from ledger.projection import LocalProjection


class EngineContract:
    postgres = False

    def setUp(self):
        if self.postgres:
            import psycopg
            self.schema = 'test_' + uuid.uuid4().hex
            self.base_dsn = os.environ['TEST_DATABASE_URL']
            with psycopg.connect(self.base_dsn, autocommit=True) as conn:
                conn.execute(f'CREATE SCHEMA {self.schema}')
            self.db = Database('postgres', self.base_dsn + f" options='-c search_path={self.schema}'")
        else:
            self.temp = tempfile.TemporaryDirectory(prefix='ledger-engine-')
            self.db = Database('sqlite', Path(self.temp.name) / 'test.db')
        self.db.bootstrap()
        self.service = Ledger(self.db, LocalProjection(self.db))
        self.service.worker_paused = True

    def tearDown(self):
        if self.postgres:
            import psycopg
            with psycopg.connect(self.base_dsn, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA {self.schema} CASCADE')
        else:
            self.temp.cleanup()

    def create(self, balance=10000, owner='alice'):
        return self.service.create_account(owner, str(uuid.uuid4()), {
            'name':'Checking', 'full_name': owner.title()+' Example',
            'date_of_birth':'2004-01-15','initial_balance_cents':balance})['account']['id']

    def balance(self, account, owner='alice'):
        return self.service.get_account(owner, account)['balance_cents']

    def assert_clean(self):
        self.service.worker_paused = False
        while self.service.drain_outbox(500)['processed']:
            pass
        report = self.service.reconcile()
        self.assertTrue(report['ok'], report)
        self.assertEqual([], report['projection_lag'])
        self.assertEqual(0, report['pending_events'])

    def test_required_opening_deposit_withdrawal_flow(self):
        account = self.create()
        self.service.deposit('alice', account, 'deposit-test-0001', {'amount_cents':2500})
        self.service.withdraw('alice', account, 'withdraw-test-001', {'amount_cents':4000})
        self.assertEqual(8500, self.balance(account))
        self.assertEqual(3, len(self.service.list_transactions('alice', account)))
        self.assert_clean()

    def test_concurrent_withdrawals_cannot_overspend(self):
        account = self.create()
        def withdraw(i):
            try:
                self.service.withdraw('alice', account, f'withdraw-concurrent-{i}', {'amount_cents':1000})
                return 'success'
            except LedgerError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(withdraw, range(100)))
        self.assertEqual(10, results.count('success'))
        self.assertEqual(90, results.count('insufficient_funds'))
        self.assertEqual(0, self.balance(account))
        self.assert_clean()

    def test_simultaneous_same_key_applies_only_once(self):
        account = self.create()
        def deposit(_):
            return self.service.deposit('alice', account, 'one-simultaneous-key',
                {'amount_cents':500})['transaction_id']
        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(deposit, range(24)))
        self.assertEqual(1, len(set(ids)))
        self.assertEqual(10500, self.balance(account))
        self.assert_clean()

    def test_same_key_changed_payload_conflicts(self):
        account = self.create()
        self.service.deposit('alice', account, 'same-key-conflict', {'amount_cents':100})
        with self.assertRaises(LedgerError) as ctx:
            self.service.deposit('alice', account, 'same-key-conflict', {'amount_cents':101})
        self.assertEqual('idempotency_conflict', ctx.exception.code)
        self.assertEqual(10100, self.balance(account))

    def test_opposing_transfers_preserve_money(self):
        a, b = self.create(100000), self.create(100000, 'bob')
        def transfer(i):
            source,dest,owner = (a,b,'alice') if i%2 else (b,a,'bob')
            return self.service.transfer(owner, f'opposing-transfer-{i}',
                {'source_account_id':source,'destination_account_id':dest,
                 'amount_cents':100})
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(transfer, range(60)))
        self.assertEqual(100000, self.balance(a))
        self.assertEqual(100000, self.balance(b,'bob'))
        self.assert_clean()

    def test_unauthorized_transfer_rolls_back(self):
        a, b = self.create(), self.create(owner='bob')
        with self.assertRaises(LedgerError) as ctx:
            self.service.transfer('bob','unauthorized-transfer',
                {'source_account_id':a,'destination_account_id':b,'amount_cents':100})
        self.assertEqual(403, ctx.exception.status)
        self.assertEqual(10000, self.balance(a))
        self.assertEqual(10000, self.balance(b,'bob'))

    def test_failure_before_commit_leaves_no_partial_operation(self):
        account = self.create()
        def fault(point):
            if point == 'before_commit':
                raise RuntimeError('Injected before commit')
        self.service.fault_hook = fault
        with self.assertRaises(RuntimeError):
            self.service.deposit('alice',account,'before-commit-fault',{'amount_cents':700})
        self.assertEqual(10000, self.balance(account))
        self.service.fault_hook = None
        self.service.deposit('alice',account,'before-commit-fault',{'amount_cents':700})
        self.assertEqual(10700, self.balance(account))
        self.assert_clean()

    def test_lost_response_after_commit_retries_without_duplicate(self):
        account = self.create()
        def fault(point):
            if point == 'after_commit':
                raise RuntimeError('Injected lost response')
        self.service.fault_hook = fault
        with self.assertRaises(RuntimeError):
            self.service.deposit('alice',account,'after-commit-fault',{'amount_cents':700})
        self.assertEqual(10700, self.balance(account))
        self.service.fault_hook = None
        first = self.service.deposit('alice',account,'after-commit-fault',{'amount_cents':700})
        second = self.service.deposit('alice',account,'after-commit-fault',{'amount_cents':700})
        self.assertEqual(first['transaction_id'],second['transaction_id'])
        self.assertEqual(10700,self.balance(account))
        self.assert_clean()

    def test_outage_does_not_undo_committed_financial_state(self):
        account = self.create()
        original = self.service.projection
        class Offline:
            def put(self, snapshot):
                raise ConnectionError('simulated projection outage')
            def get(self, account):
                raise ConnectionError('simulated projection outage')
        self.service.projection = Offline()
        self.service.worker_paused = False
        result = self.service.deposit('alice',account,'projection-down-test',{'amount_cents':500})
        self.assertFalse(result['projection_synced'])
        self.assertEqual(10500,self.balance(account))
        self.service.projection = original
        self.assert_clean()

    def test_stale_and_duplicate_snapshots_cannot_regress_balance(self):
        account = self.create()
        self.service.deposit('alice',account,'out-of-order-test',{'amount_cents':300})
        with self.db.transaction(False) as tx:
            events=tx.execute('SELECT payload FROM outbox_events WHERE account_id=:id ORDER BY version',{'id':account})
        old,new=[json.loads(e['payload']) for e in events]
        self.service.projection.put(new)
        self.service.projection.put(old)
        self.service.projection.put(new)
        self.assertEqual(10300,self.service.projection.get(account)['balance_cents'])
        self.assert_clean()

    def test_crash_after_projection_write_replays_safely(self):
        account=self.create()
        self.service.worker_paused=False
        def fault(point):
            if point=='after_projection_write':
                raise RuntimeError('worker crashed after writing summary')
        self.service.fault_hook=fault
        self.assertEqual(1,self.service.drain_outbox()['failed'])
        self.service.fault_hook=None
        self.assertEqual(1,self.service.drain_outbox()['processed'])
        self.assertEqual(10000,self.service.projection.get(account)['balance_cents'])
        self.assert_clean()

    def test_history_is_immutable(self):
        account=self.create()
        with self.assertRaises(Exception):
            with self.db.transaction() as tx:
                tx.execute("UPDATE ledger_entries SET amount_cents=1 WHERE account_id=:id",{'id':account})
        self.assert_clean()

    def test_reconciliation_detects_corrupted_stored_balance(self):
        account=self.create()
        with self.db.transaction() as tx:
            tx.execute('UPDATE accounts SET balance_cents=9000 WHERE id=:id',{'id':account})
        report=self.service.reconcile()
        self.assertFalse(report['ok'])
        self.assertEqual('ledger_balance',report['mismatches'][0]['kind'])

    def test_reconciliation_detects_impossible_future_projection(self):
        account=self.create()
        snapshot=self.service.get_account('alice',account)
        snapshot.update(version=snapshot['version']+100,balance_cents=999999)
        self.service.projection.put(snapshot)
        report=self.service.reconcile()
        self.assertFalse(report['ok'])
        self.assertEqual('summary_ahead',report['mismatches'][0]['kind'])


class SQLiteEngineTests(EngineContract, unittest.TestCase):
    pass


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'Set TEST_DATABASE_URL for real PostgreSQL tests')
class PostgreSQLEngineTests(EngineContract, unittest.TestCase):
    postgres=True


if __name__=='__main__':
    unittest.main()
