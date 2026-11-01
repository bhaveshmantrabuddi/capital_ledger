"""Versioned account snapshots; updates never apply monetary deltas twice."""
import json


class LocalProjection:
    def __init__(self, database):
        self.db = database

    def put(self, snapshot):
        with self.db.transaction() as tx:
            tx.execute('''INSERT INTO summary_accounts(account_id,version,payload)
                VALUES (:account_id,:version,:payload)
                ON CONFLICT (account_id) DO UPDATE SET
                version=excluded.version,payload=excluded.payload
                WHERE summary_accounts.version < excluded.version''',
                {'account_id': snapshot['id'], 'version': snapshot['version'],
                 'payload': json.dumps(snapshot, sort_keys=True)})

    def get(self, account_id):
        with self.db.transaction(False) as tx:
            row = tx.one('SELECT payload FROM summary_accounts WHERE account_id=:id',
                         {'id': account_id})
        return json.loads(row['payload']) if row else None


class DynamoProjection:
    def __init__(self, table_name, table=None):
        if table is None:
            import boto3
            table = boto3.resource('dynamodb').Table(table_name)
        self.table = table

    def put(self, snapshot):
        item = dict(snapshot, account_id=snapshot['id'])
        try:
            self.table.put_item(Item=item,
                ConditionExpression='attribute_not_exists(#v) OR #v < :v',
                ExpressionAttributeNames={'#v': 'version'},
                ExpressionAttributeValues={':v': snapshot['version']})
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
                raise

    def get(self, account_id):
        item = self.table.get_item(Key={'account_id': account_id},
                                   ConsistentRead=True).get('Item')
        if item:
            for field in ('balance_cents', 'version', 'system_account'):
                if field in item:
                    item[field] = int(item[field])
        return item
