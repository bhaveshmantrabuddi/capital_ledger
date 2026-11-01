"""HTTP-independent API routing. Authorization happens before entering this module."""
from urllib.parse import parse_qs
import re
from .engine import LedgerError


def dispatch(service, method, path, owner, body, key=None, query=''):
    if method == 'GET' and path == '/api/me':
        return 200, {'user_id': owner}
    if path == '/api/accounts':
        if method == 'GET':
            return 200, {'accounts': service.list_accounts(owner)}
        if method == 'POST':
            return 201, service.create_account(owner, key, body)
    match = re.fullmatch(r'/api/accounts/([a-zA-Z0-9\-]+)', path)
    if match and method == 'GET':
        return 200, {'account': service.get_account(owner, match[1])}
    match = re.fullmatch(r'/api/accounts/([a-zA-Z0-9\-]+)/(deposits|withdrawals)', path)
    if match and method == 'POST':
        fn = service.deposit if match[2] == 'deposits' else service.withdraw
        return 201, fn(owner, match[1], key, body)
    if path == '/api/transfers' and method == 'POST':
        return 201, service.transfer(owner, key, body)
    if path == '/api/transactions' and method == 'GET':
        args = parse_qs(query)
        return 200, {'transactions': service.list_transactions(owner, args.get('account_id', [None])[0])}
    raise LedgerError('not_found', 'Endpoint not found.', 404)
