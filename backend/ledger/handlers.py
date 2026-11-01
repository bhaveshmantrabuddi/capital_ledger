"""AWS Lambda entry points. Customer authentication is verified by API Gateway JWT auth."""
import base64
from functools import lru_cache
import json
import logging
import os
from .api import dispatch
from .database import Database
from .engine import Ledger, LedgerError
from .projection import DynamoProjection

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


@lru_cache(maxsize=1)
def service():
    backend = os.environ.get('DB_BACKEND')
    if backend not in ('aurora', 'aurora_iam') or os.environ.get('AUTH_MODE') != 'cognito':
        raise RuntimeError('AWS handlers require Aurora and verified Cognito authentication.')
    if backend == 'aurora_iam':
        db = Database('aurora_iam', host=os.environ['AURORA_HOST'],
            port=int(os.environ.get('AURORA_PORT', '5432')),
            username=os.environ.get('AURORA_USERNAME', 'postgres'),
            database=os.environ.get('AURORA_DATABASE', 'postgres'),
            connect_timeout=int(os.environ.get('AURORA_CONNECT_TIMEOUT', '10')))
    else:
        db = Database('aurora', cluster_arn=os.environ['AURORA_CLUSTER_ARN'],
                      secret_arn=os.environ['AURORA_SECRET_ARN'],
                      database=os.environ.get('AURORA_DATABASE', 'ledger'))
    return Ledger(db, DynamoProjection(os.environ['ACCOUNTS_TABLE']))


def response(status, body):
    return {'statusCode': status,
            'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store',
                        'X-Content-Type-Options': 'nosniff'},
            'body': json.dumps(body, allow_nan=False)}


def _http(event, account_function, context=None):
    try:
        method = event.get('requestContext', {}).get('http', {}).get('method', '')
        path = event.get('rawPath', '')
        if account_function and path == '/api/health' and method == 'GET':
            return response(200, {'mode': 'aws', 'auth_mode': 'cognito', 'simulation': True})
        # There is no development-login branch in a deployed Lambda.
        jwt = event.get('requestContext', {}).get('authorizer', {}).get('jwt', {})
        claims = jwt.get('claims', {})
        owner = claims.get('sub')
        scopes = set(str(claims.get('scope') or '').split()) | set(jwt.get('scopes') or [])
        if not owner or 'ledger-api/access' not in scopes:
            raise LedgerError('unauthorized', 'A valid scoped access token is required.', 401)
        if path.startswith('/api/dev/') or path == '/api/reconciliation':
            raise LedgerError('not_found', 'Endpoint not found.', 404)
        is_account_path = path in ('/api/me','/api/accounts') or (
            path.startswith('/api/accounts/') and path.count('/') == 3)
        if account_function != is_account_path:
            raise LedgerError('not_found', 'Endpoint not found.', 404)
        headers = {k.lower(): v for k, v in event.get('headers', {}).items()}
        raw = event.get('body') or '{}'
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw).decode('utf-8')
        if len(raw.encode('utf-8')) > 65536:
            raise LedgerError('payload_too_large', 'Request body is too large.', 413)
        try:
            body = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise LedgerError('invalid_json', 'Request body must be a JSON object.')
        status, result = dispatch(service(), method, path, owner, body,
                                  headers.get('idempotency-key'), event.get('rawQueryString',''))
        if method == 'POST' and status == 201:
            # SQL has already committed. A notification failure must never turn a
            # confirmed financial result into an apparent failed transaction.
            try:
                remaining = getattr(context, 'get_remaining_time_in_millis', None)
                if callable(remaining) and remaining() < 6000:
                    LOG.warning(json.dumps({'event': 'maintenance_notification_deferred',
                                            'count': 1, 'reason': 'lambda_deadline'}))
                else:
                    enqueue_maintenance('committed_mutation')
            except Exception as exc:
                LOG.warning(json.dumps({'event': 'maintenance_notification_failed',
                                        'error_type': type(exc).__name__}))
        return response(status, result)
    except LedgerError as exc:
        return response(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
    except Exception:
        LOG.exception('request_failed')
        return response(500, {'error': {'code': 'internal_error',
            'message': 'Request failed. Retry mutations with the same idempotency key.'}})


def accounts_handler(event, context):
    return _http(event, True, context)


def transactions_handler(event, context):
    return _http(event, False, context)


@lru_cache(maxsize=1)
def queue_client():
    import boto3
    from botocore.config import Config
    return boto3.client('sqs', config=Config(connect_timeout=2, read_timeout=3,
        retries={'total_max_attempts': 1, 'mode': 'standard'}))


def enqueue_maintenance(reason):
    """Publish only a wake-up signal; the durable SQL outbox owns the data."""
    queue_url = os.environ.get('OUTBOX_QUEUE_URL')
    if not queue_url:
        return False
    queue_client().send_message(QueueUrl=queue_url,
        MessageBody=json.dumps({'version': 1, 'reason': reason}))
    return True


def maintenance_handler(event, context):
    """Private SQS entry point; failed batches retry and eventually reach a DLQ.

    Messages are wake-up signals, not commands to move money. Each invocation
    drains at most 100 SQL events and only a full batch schedules one successor.
    A once-daily signal recovers commits whose immediate notification was lost.
    Default AWS recursion safeguards remain enabled. An unusually long chain can
    require an independent API/daily signal or investigation of its DLQ message.
    """
    if not isinstance(event, dict) or 'requestContext' in event or 'rawPath' in event:
        raise ValueError('Maintenance is not an HTTP endpoint.')
    records = event.get('Records')
    if records is not None and (not isinstance(records, list) or not records or
            any(not isinstance(record, dict) or record.get('eventSource') != 'aws:sqs'
                for record in records)):
        raise ValueError('Expected an SQS event or a private direct invocation.')
    ledger = service()
    batch = ledger.drain_outbox(limit=100)
    LOG.info(json.dumps({'event': 'maintenance_outbox', **batch}))
    if batch.get('paused') or batch.get('failed', 0):
        raise RuntimeError('Outbox delivery remains incomplete; retry this batch.')
    report = ledger.reconcile()
    summary = {'ok': report.get('ok') is True,
               'accounts_checked': report.get('accounts_checked', 0),
               'mismatch_count': len(report.get('mismatches') or []),
               'projection_lag_count': len(report.get('projection_lag') or []),
               'pending_events': report.get('pending_events', 0)}
    # Never put customer identifiers, profile fields, or account balances in
    # maintenance logs. A lagging summary is distinct from a financial mismatch.
    LOG.info(json.dumps({'event': 'maintenance_reconciliation', **summary}))
    if not summary['ok']:
        raise RuntimeError('Ledger reconciliation found a financial discrepancy.')
    continuation = batch.get('processed', 0) >= 100
    if continuation and not enqueue_maintenance('outbox_continuation'):
        # Do not acknowledge a full batch if no future invocation can drain the
        # remainder. Reprocessing delivered snapshots is safe and monotonic.
        # Reconcile first so a financial mismatch cannot spawn another chain.
        raise RuntimeError('A continuation queue is required for the remaining outbox.')
    return {'outbox': batch, 'reconciliation': summary, 'continuation': continuation}


def worker_handler(event, context):
    result = service().drain_outbox(limit=100)
    LOG.info(json.dumps({'event':'outbox_batch', **result}))
    return result


def reconcile_handler(event, context):
    report = service().reconcile()
    LOG.info(json.dumps({'event': 'reconciliation', **report}))
    return report


def bootstrap_handler(event, context):
    service().db.bootstrap()
    return {'ok': True, 'schema_version': 1}
