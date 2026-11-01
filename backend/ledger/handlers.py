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
            database=os.environ.get('AURORA_DATABASE', 'postgres'))
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


def _http(event, account_function):
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
        return response(status, result)
    except LedgerError as exc:
        return response(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
    except Exception:
        LOG.exception('request_failed')
        return response(500, {'error': {'code': 'internal_error',
            'message': 'Request failed. Retry mutations with the same idempotency key.'}})


def accounts_handler(event, context):
    return _http(event, True)


def transactions_handler(event, context):
    return _http(event, False)


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
