"""IAM gateway boundaries; tests never contact AWS or a remote database."""
import importlib.util
import json
import os
import unittest
from unittest.mock import Mock, patch
from ledger.database import Database
from ledger import handlers


@unittest.skipUnless(importlib.util.find_spec('psycopg'), 'Optional PostgreSQL driver not installed')
class AuroraIAMTests(unittest.TestCase):
    def make_database(self):
        signer = Mock()
        signer.generate_db_auth_token.side_effect = ['temporary-one', 'temporary-two']
        db = Database('aurora_iam', host='sample.cluster.test.rds.amazonaws.com',
            database='postgres', username='postgres', client=signer)
        return db, signer

    def test_connections_sign_fresh_tokens_and_require_verified_tls(self):
        db, signer = self.make_database()
        conn = Mock()
        conn.execute.return_value.description = None
        with patch('psycopg.connect', return_value=conn) as connect:
            for _ in range(2):
                with db.transaction() as tx:
                    tx.execute('UPDATE accounts SET balance_cents=:value WHERE id=:id',
                               {'value': 100, 'id': 'fictional-account'})
        self.assertEqual(signer.generate_db_auth_token.call_count, 2)
        signer.generate_db_auth_token.assert_called_with(
            DBHostname='sample.cluster.test.rds.amazonaws.com', Port=5432, DBUsername='postgres')
        self.assertEqual([c.kwargs['password'] for c in connect.call_args_list],
                         ['temporary-one', 'temporary-two'])
        for call in connect.call_args_list:
            self.assertEqual(call.kwargs['sslmode'], 'verify-full')
            self.assertTrue(call.kwargs['sslrootcert'].endswith('aws-trust-ca.pem'))
        conn.execute.assert_any_call(
            'UPDATE accounts SET balance_cents=%(value)s WHERE id=%(id)s',
            {'value': 100, 'id': 'fictional-account'})
        self.assertEqual(conn.commit.call_count, 2)
        self.assertEqual(conn.close.call_count, 2)
        self.assertNotIn('temporary', db.location)

    def test_failure_rolls_back_and_closes_connection(self):
        db, _ = self.make_database()
        conn = Mock()
        with patch('psycopg.connect', return_value=conn):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                with db.transaction():
                    raise RuntimeError('injected transaction failure')
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        conn.close.assert_called_once()

    def test_endpoint_is_required(self):
        with self.assertRaisesRegex(ValueError, 'endpoint'):
            Database('aurora_iam', client=Mock())


class IAMHandlerConfigurationTests(unittest.TestCase):
    def test_iam_mode_requires_no_managed_master_password(self):
        env = {'DB_BACKEND':'aurora_iam', 'AUTH_MODE':'cognito',
               'AURORA_HOST':'sample.cluster.test.rds.amazonaws.com',
               'AURORA_DATABASE':'postgres', 'ACCOUNTS_TABLE':'fictional-table'}
        handlers.service.cache_clear()
        try:
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(handlers, 'Database') as database, \
                 patch.object(handlers, 'DynamoProjection'):
                handlers.service()
                database.assert_called_once_with('aurora_iam',
                    host='sample.cluster.test.rds.amazonaws.com',port=5432,
                    username='postgres',database='postgres')
        finally:
            handlers.service.cache_clear()


if __name__ == '__main__':
    unittest.main()
