"""Small parameterized-SQL adapters. Each context owns its transaction/connection."""
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3


class Session:
    def __init__(self, execute, locking):
        self._execute = execute
        self.locking = locking

    def execute(self, sql, params=None):
        return self._execute(sql, params or {})

    def one(self, sql, params=None):
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    def lock(self, sql):
        return sql + (' FOR UPDATE' if self.locking else '')


class Database:
    def __init__(self, backend='sqlite', location='ledger.db', *, cluster_arn=None,
                 secret_arn=None, database='ledger', client=None, host=None,
                 port=5432, username='postgres', sslrootcert=None, connect_timeout=10):
        self.backend = backend
        self.location = str(location)
        self.cluster_arn = cluster_arn
        self.secret_arn = secret_arn
        self.database = database
        self.client = client
        self.host, self.port, self.username = host, int(port), username
        if isinstance(connect_timeout, bool) or not isinstance(connect_timeout, int) or not 1 <= connect_timeout <= 60:
            raise ValueError('Connection timeout must be an integer from 1 to 60 seconds.')
        self.connect_timeout = connect_timeout
        self.sslrootcert = sslrootcert or str(Path(__file__).with_name('aws-trust-ca.pem'))
        if backend == 'aurora_iam':
            if not host:
                raise ValueError('Aurora IAM mode requires a database endpoint.')
            if client is None:
                import boto3
                self.client = boto3.client('rds')
        if backend == 'aurora' and client is None:
            import boto3
            from botocore.config import Config
            # Retrying a statement after a lost response could apply its UPDATE twice
            # inside one transaction. Only retry a whole idempotent API operation.
            self.client = boto3.client('rds-data', config=Config(
                connect_timeout=3, read_timeout=12,
                retries={'total_max_attempts': 1, 'mode': 'standard'}))
        if backend not in ('sqlite', 'postgres', 'aurora', 'aurora_iam'):
            raise ValueError('DB_BACKEND must be sqlite, postgres, aurora, or aurora_iam')

    @contextmanager
    def transaction(self, write=True):
        if self.backend == 'aurora':
            args = dict(resourceArn=self.cluster_arn, secretArn=self.secret_arn,
                        database=self.database)
            tid = self.client.begin_transaction(**args)['transactionId']

            def run(sql, params):
                result = self.client.execute_statement(
                    **args, transactionId=tid, sql=sql,
                    parameters=[self._parameter(k, v) for k, v in params.items()],
                    includeResultMetadata=True)
                names = [c['name'] for c in result.get('columnMetadata', [])]
                return [dict(zip(names, [self._field(v) for v in row]))
                        for row in result.get('records', [])]
            try:
                if not write:
                    run('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY', {})
                run("SET LOCAL lock_timeout = '2000ms'", {})
                run("SET LOCAL statement_timeout = '8000ms'", {})
                yield Session(run, True)
                self.client.commit_transaction(resourceArn=self.cluster_arn,
                    secretArn=self.secret_arn, transactionId=tid)
            except BaseException:
                try:
                    self.client.rollback_transaction(resourceArn=self.cluster_arn,
                        secretArn=self.secret_arn, transactionId=tid)
                except Exception:
                    pass
                raise
            return
        if self.backend in ('postgres', 'aurora_iam'):
            import psycopg
            from psycopg.rows import dict_row
            if self.backend == 'aurora_iam':
                token = self.client.generate_db_auth_token(DBHostname=self.host,
                    Port=self.port, DBUsername=self.username)
                conn = psycopg.connect(host=self.host, port=self.port,
                    user=self.username, dbname=self.database, password=token,
                    sslmode='verify-full', sslrootcert=self.sslrootcert,
                    row_factory=dict_row, connect_timeout=self.connect_timeout)
            else:
                conn = psycopg.connect(self.location, row_factory=dict_row,
                                       connect_timeout=self.connect_timeout)
        else:
            conn = sqlite3.connect(self.location, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row

        def run(sql, params):
            if self.backend in ('postgres', 'aurora_iam'):
                sql = re.sub(r'(?<!:):([a-zA-Z_][a-zA-Z_0-9]*)', r'%(\1)s', sql)
            cursor = conn.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()] if cursor.description else []
        try:
            if self.backend in ('postgres', 'aurora_iam'):
                if not write:
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                conn.execute("SET LOCAL lock_timeout = '2000ms'")
                conn.execute("SET LOCAL statement_timeout = '8000ms'")
            else:
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute('PRAGMA busy_timeout=30000')
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield Session(run, self.backend in ('postgres', 'aurora_iam'))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _parameter(name, value):
        if value is None:
            field = {'isNull': True}
        elif isinstance(value, bool):
            field = {'booleanValue': value}
        elif isinstance(value, int):
            field = {'longValue': value}
        else:
            field = {'stringValue': str(value)}
        return {'name': name, 'value': field}

    @staticmethod
    def _field(field):
        if field.get('isNull'):
            return None
        for key in ('stringValue', 'longValue', 'doubleValue', 'booleanValue'):
            if key in field:
                return field[key]
        raise ValueError('Unsupported Data API field')

    def bootstrap(self):
        schema = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')
        with self.transaction() as tx:
            for statement in schema.split(';'):
                if statement.strip():
                    tx.execute(statement)
            tx.execute('''INSERT INTO accounts
                (id,owner_id,name,full_name,date_of_birth,balance_cents,currency,
                 version,system_account,created_at)
                VALUES ('system-funding','system','Simulation funding','System','',0,
                        'USD',1,1,'2026-01-01T00:00:00+00:00')
                ON CONFLICT (id) DO NOTHING''')
            # Application writes are append-only; triggers also reject accidental SQL edits.
            if self.backend == 'sqlite':
                for table in ('transactions', 'ledger_entries'):
                    for operation in ('UPDATE', 'DELETE'):
                        tx.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation}
                            BEFORE {operation} ON {table} BEGIN
                            SELECT RAISE(ABORT, 'ledger history is immutable'); END''')
            else:
                tx.execute('''CREATE OR REPLACE FUNCTION ledger_reject_mutation()
                    RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    RAISE EXCEPTION 'ledger history is immutable'; END; $$''')
                for table in ('transactions', 'ledger_entries'):
                    tx.execute(f'DROP TRIGGER IF EXISTS immutable_{table} ON {table}')
                    tx.execute(f'''CREATE TRIGGER immutable_{table}
                        BEFORE UPDATE OR DELETE ON {table}
                        FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation()''')

    @staticmethod
    def retryable(exc):
        return getattr(exc, 'sqlstate', None) in ('40001', '40P01', '55P03')
