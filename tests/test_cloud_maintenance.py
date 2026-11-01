"""Cloud maintenance contracts; real local SQL state, fake SQS transport."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ledger import handlers
from ledger.database import Database
from ledger.engine import Ledger
from ledger.projection import LocalProjection


def sqs_event():
    return {'Records': [{'eventSource': 'aws:sqs', 'messageId': 'signal-1',
                         'body': '{"version":1,"reason":"committed_mutation"}'}]}


def request(path, method='POST', body=None, key='maintenance-deposit-001', authorized=True):
    context = {'http': {'method': method}}
    if authorized:
        context['authorizer'] = {'jwt': {'claims': {
            'sub': 'fictional-owner', 'scope': 'ledger-api/access'}}}
    return {'rawPath': path, 'requestContext': context,
            'headers': {'idempotency-key': key}, 'body': json.dumps(body or {})}


class CloudMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ledger-maintenance-')
        self.addCleanup(self.temp.cleanup)
        self.db = Database('sqlite', Path(self.temp.name) / 'ledger.db')
        self.db.bootstrap()
        self.projection = LocalProjection(self.db)
        self.ledger = Ledger(self.db, self.projection)
        self.ledger.worker_paused = True
        created = self.ledger.create_account('fictional-owner', 'maintenance-opening-001', {
            'name': 'Recovery test', 'full_name': 'Fictional Test User',
            'date_of_birth': '2000-01-01', 'initial_balance_cents': 10000})
        self.account_id = created['account']['id']
        self.queue = Mock()
        self.patches = [patch.object(handlers, 'service', return_value=self.ledger),
                        patch.object(handlers, 'queue_client', return_value=self.queue),
                        patch.dict(os.environ, {'OUTBOX_QUEUE_URL': 'https://sqs.example.test/queue'})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def balance(self):
        with self.db.transaction(False) as tx:
            return tx.one('SELECT balance_cents FROM accounts WHERE id=:id',
                          {'id': self.account_id})['balance_cents']

    def transaction_count(self):
        return len(self.ledger.list_transactions('fictional-owner', self.account_id))

    def test_lost_notification_keeps_committed_result_and_replay_is_one_operation(self):
        self.queue.send_message.side_effect = ConnectionError('synthetic queue outage')
        event = request('/api/accounts/' + self.account_id + '/deposits', body={'amount_cents': 500})
        with self.assertLogs(handlers.LOG, level='WARNING'):
            first = handlers.transactions_handler(event, None)
            replay = handlers.transactions_handler(event, None)
        self.assertEqual(first['statusCode'], 201)
        self.assertEqual(replay['statusCode'], 201)
        self.assertEqual(json.loads(first['body'])['transaction_id'],
                         json.loads(replay['body'])['transaction_id'])
        self.assertEqual(self.balance(), 10500)
        self.assertEqual(self.transaction_count(), 2)
        self.assertEqual(self.queue.send_message.call_count, 2)
        self.ledger.worker_paused = False
        self.queue.send_message.side_effect = None
        # The daily signal can recover a commit even if both notifications failed.
        result = handlers.maintenance_handler(sqs_event(), None)
        self.assertEqual(result['reconciliation']['pending_events'], 0)
        self.assertEqual(self.projection.get(self.account_id)['balance_cents'], 10500)

    def test_notification_contains_only_generic_signal_not_customer_or_financial_data(self):
        event = request('/api/accounts/' + self.account_id + '/deposits', body={'amount_cents': 500})
        self.assertEqual(handlers.transactions_handler(event, None)['statusCode'], 201)
        args = self.queue.send_message.call_args.kwargs
        self.assertEqual(json.loads(args['MessageBody']),
                         {'version': 1, 'reason': 'committed_mutation'})
        for private in (self.account_id, 'fictional-owner', '500', 'Fictional Test User'):
            self.assertNotIn(private, args['MessageBody'])

    def test_near_deadline_preserves_the_committed_response_and_defers_notification(self):
        context = Mock()
        context.get_remaining_time_in_millis.return_value = 5999
        event = request('/api/accounts/' + self.account_id + '/deposits', body={'amount_cents': 500})
        with self.assertLogs(handlers.LOG, level='WARNING') as logs:
            result = handlers.transactions_handler(event, context)
        self.assertEqual(result['statusCode'], 201)
        self.assertEqual(self.balance(), 10500)
        self.assertEqual(self.transaction_count(), 2)
        self.queue.send_message.assert_not_called()
        context.get_remaining_time_in_millis.assert_called_once_with()
        self.assertIn('maintenance_notification_deferred', '\n'.join(logs.output))

    def test_get_validation_failure_and_unauthorized_requests_do_not_enqueue(self):
        for event in (request('/api/me', 'GET'),
                      request('/api/accounts/' + self.account_id + '/deposits', body={'amount_cents': -1}),
                      request('/api/accounts', body={}, authorized=False)):
            response = handlers.accounts_handler(event, None) if event['rawPath'] in (
                '/api/me', '/api/accounts') else handlers.transactions_handler(event, None)
            self.assertIn(response['statusCode'], (200, 400, 401))
        self.queue.send_message.assert_not_called()
        self.assertEqual(self.balance(), 10000)

    def test_optional_queue_does_not_change_legacy_handler_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            event = request('/api/accounts/' + self.account_id + '/deposits', body={'amount_cents': 500})
            self.assertEqual(handlers.transactions_handler(event, None)['statusCode'], 201)
        self.queue.send_message.assert_not_called()
        self.assertEqual(self.balance(), 10500)

    def test_delivery_failure_raises_then_redelivery_recovers_without_moving_money(self):
        broken = Mock(wraps=self.projection)
        broken.put.side_effect = ConnectionError('synthetic DynamoDB outage')
        self.ledger.projection = broken
        self.ledger.worker_paused = False
        with self.assertRaisesRegex(RuntimeError, 'retry this batch'):
            handlers.maintenance_handler(sqs_event(), None)
        self.assertEqual(self.balance(), 10000)
        self.assertEqual(self.transaction_count(), 1)
        self.ledger.projection = self.projection
        fixed = handlers.maintenance_handler(sqs_event(), None)
        duplicate = handlers.maintenance_handler(sqs_event(), None)
        self.assertEqual(fixed['outbox']['processed'], 1)
        self.assertEqual(duplicate['outbox']['processed'], 0)
        self.assertEqual(self.transaction_count(), 1)
        self.assertEqual(self.balance(), 10000)
        self.assertEqual(fixed['reconciliation']['mismatch_count'], 0)

    def test_backlog_continuation_then_duplicate_signal_is_harmless(self):
        for index in range(101):
            self.ledger.deposit('fictional-owner', self.account_id,
                'maintenance-backlog-%03d' % index, {'amount_cents': 1})
        self.ledger.worker_paused = False
        first = handlers.maintenance_handler(sqs_event(), None)
        self.assertTrue(first['continuation'])
        self.assertEqual(first['outbox']['processed'], 100)
        self.assertEqual(first['reconciliation']['pending_events'], 2)
        self.assertTrue(first['reconciliation']['ok'])
        self.assertEqual(first['reconciliation']['projection_lag_count'], 1)
        self.assertEqual(json.loads(self.queue.send_message.call_args.kwargs['MessageBody'])['reason'],
                         'outbox_continuation')
        second = handlers.maintenance_handler(sqs_event(), None)
        repeated = handlers.maintenance_handler(sqs_event(), None)
        self.assertEqual(second['outbox']['processed'], 2)
        self.assertEqual(second['reconciliation']['pending_events'], 0)
        self.assertEqual(second['reconciliation']['projection_lag_count'], 0)
        self.assertEqual(repeated['outbox']['processed'], 0)
        self.assertEqual(self.transaction_count(), 102)
        self.assertEqual(self.balance(), 10101)

    def test_continuation_failure_does_not_acknowledge_remaining_work(self):
        self.ledger.worker_paused = False
        with patch.object(self.ledger, 'drain_outbox', return_value={
                'processed': 100, 'failed': 0, 'paused': False}), \
             patch.object(handlers, 'enqueue_maintenance', side_effect=ConnectionError('queue outage')):
            with self.assertRaises(ConnectionError):
                handlers.maintenance_handler(sqs_event(), None)

    def test_missing_continuation_queue_does_not_acknowledge_remaining_work(self):
        with patch.object(self.ledger, 'drain_outbox', return_value={
                'processed': 100, 'failed': 0, 'paused': False}), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'continuation queue'):
                handlers.maintenance_handler({}, None)

    def test_reconciliation_logs_counts_and_financial_discrepancy_fails_batch(self):
        self.ledger.worker_paused = False
        report = {'ok': False, 'accounts_checked': 2,
                  'mismatches': [{'account_id': 'private-account', 'stored': 12345}],
                  'projection_lag': [], 'pending_events': 0}
        with patch.object(self.ledger, 'reconcile', return_value=report), \
             self.assertLogs(handlers.LOG, level='INFO') as logs:
            with self.assertRaisesRegex(RuntimeError, 'financial discrepancy'):
                handlers.maintenance_handler({}, None)
        output = '\n'.join(logs.output)
        self.assertIn('"mismatch_count": 1', output)
        self.assertNotIn('private-account', output)
        self.assertNotIn('12345', output)
        self.assertNotIn('Fictional Test User', output)

    def test_financial_discrepancy_stops_even_a_full_batch_from_spawning_continuations(self):
        report = {'ok': False, 'accounts_checked': 2,
                  'mismatches': [{'kind': 'unbalanced_transaction'}],
                  'projection_lag': [], 'pending_events': 5}
        with patch.object(self.ledger, 'drain_outbox', return_value={
                'processed': 100, 'failed': 0, 'paused': False}) as drain, \
             patch.object(self.ledger, 'reconcile', return_value=report):
            with self.assertRaisesRegex(RuntimeError, 'financial discrepancy'):
                handlers.maintenance_handler(sqs_event(), None)
        drain.assert_called_once_with(limit=100)
        self.queue.send_message.assert_not_called()

    def test_partial_and_empty_batches_stop_without_scheduling_another_invocation(self):
        self.ledger.worker_paused = False
        for expected_processed in (1, 0):
            result = handlers.maintenance_handler(sqs_event(), None)
            self.assertEqual(result['outbox']['processed'], expected_processed)
            self.assertFalse(result['continuation'])
        self.queue.send_message.assert_not_called()

    def test_public_http_and_non_sqs_record_events_are_rejected(self):
        for event in (request('/api/reconciliation'),
                      {'Records': [{'eventSource': 'aws:s3'}]}, {'Records': []}, []):
            with self.subTest(event=event), self.assertRaises(ValueError):
                handlers.maintenance_handler(event, None)
        self.assertEqual(self.transaction_count(), 1)


@unittest.skipUnless(importlib.util.find_spec('boto3'), 'Optional AWS SDK not installed')
class QueueClientConfigurationTests(unittest.TestCase):
    def test_notification_transport_has_a_bounded_timeout_and_no_sdk_retries(self):
        handlers.queue_client.cache_clear()
        try:
            with patch('boto3.client') as client:
                handlers.queue_client()
            self.assertEqual(client.call_args.args, ('sqs',))
            config = client.call_args.kwargs['config']
            self.assertEqual(config.retries['total_max_attempts'], 1)
            self.assertLessEqual(config.connect_timeout, 2)
            self.assertLessEqual(config.read_timeout, 3)
        finally:
            handlers.queue_client.cache_clear()


if __name__ == '__main__':
    unittest.main()
