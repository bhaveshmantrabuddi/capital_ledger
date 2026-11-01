"""AWS wire-contract tests using SDK-shaped fakes; no AWS calls are made.

These verify adapter and Lambda trust boundaries. They do not substitute for a
real deployment test of Aurora, DynamoDB, API Gateway, Cognito, or IAM.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
import os
import unittest
from unittest.mock import Mock, patch

from ledger.database import Database
from ledger import handlers
from ledger.projection import DynamoProjection


class FakeSDKError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class VersionedDynamoTable:
    """Fake implements the wire condition used for this project's snapshots."""

    def __init__(self):
        self.item = None
        self.calls = []
        self.failure = None

    def put_item(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.failure:
            raise self.failure
        # Fail if an accidental unguarded put would erase the current version.
        if kwargs.get("ConditionExpression") != "attribute_not_exists(#v) OR #v < :v":
            raise AssertionError("snapshot write must have the expected monotonic-version condition")
        if kwargs.get("ExpressionAttributeNames") != {"#v": "version"}:
            raise AssertionError("version condition is not bound to the version attribute")
        incoming = kwargs["ExpressionAttributeValues"][":v"]
        if self.item is not None and self.item["version"] >= incoming:
            raise FakeSDKError("ConditionalCheckFailedException")
        self.item = deepcopy(kwargs["Item"])
        return {}

    def get_item(self, **kwargs):
        self.read = kwargs
        return {"Item": deepcopy(self.item)} if self.item is not None else {}


class DynamoAdapterTests(unittest.TestCase):
    def test_duplicate_and_old_events_cannot_overwrite_a_newer_snapshot(self):
        table = VersionedDynamoTable()
        projection = DynamoProjection("fictional-test-table", table=table)
        projection.put({"id": "account-1", "version": 5, "balance_cents": 900})
        projection.put({"id": "account-1", "version": 4, "balance_cents": 100})
        projection.put({"id": "account-1", "version": 5, "balance_cents": 200})
        self.assertEqual(table.item["version"], 5)
        self.assertEqual(table.item["balance_cents"], 900)
        projection.put({"id": "account-1", "version": 6, "balance_cents": 950})
        self.assertEqual(table.item["version"], 6)
        self.assertEqual(table.item["balance_cents"], 950)
        self.assertEqual(table.item["account_id"], "account-1")

    def test_non_conditional_sdk_errors_propagate_for_outbox_retry(self):
        table = VersionedDynamoTable()
        table.failure = FakeSDKError("ProvisionedThroughputExceededException")
        projection = DynamoProjection("fictional-test-table", table=table)
        with self.assertRaises(FakeSDKError) as raised:
            projection.put({"id": "account-1", "version": 1, "balance_cents": 100})
        self.assertIs(raised.exception, table.failure)
        self.assertIsNone(table.item)

    def test_dynamo_decimal_values_are_normalized_and_read_consistently(self):
        table = VersionedDynamoTable()
        table.item = {"account_id": "account-1", "balance_cents": Decimal("123"), "version": Decimal("3")}
        result = DynamoProjection("fictional-test-table", table=table).get("account-1")
        self.assertIs(type(result["balance_cents"]), int)
        self.assertIs(type(result["version"]), int)
        self.assertEqual(result["balance_cents"], 123)
        self.assertEqual(table.read, {"Key": {"account_id": "account-1"}, "ConsistentRead": True})


class FakeDataAPI:
    def __init__(self, fail_statement=False, fail_rollback=False):
        self.calls = []
        self.fail_statement = fail_statement
        self.fail_rollback = fail_rollback
        self.sql_failure = RuntimeError("simulated SQL failure")

    def begin_transaction(self, **kwargs):
        self.calls.append(("begin", kwargs))
        return {"transactionId": "transaction-123"}

    def execute_statement(self, **kwargs):
        self.calls.append(("execute", kwargs))
        if kwargs["sql"].startswith("UPDATE") and self.fail_statement:
            raise self.sql_failure
        if kwargs["sql"].startswith("SELECT"):
            return {"columnMetadata": [{"name": "id"}, {"name": "balance_cents"}],
                    "records": [[{"stringValue": "account-1"}, {"longValue": 123}]]}
        return {}

    def commit_transaction(self, **kwargs):
        self.calls.append(("commit", kwargs))
        return {}

    def rollback_transaction(self, **kwargs):
        self.calls.append(("rollback", kwargs))
        if self.fail_rollback:
            raise RuntimeError("simulated rollback transport failure")
        return {}


class DataAPIAdapterTests(unittest.TestCase):
    @staticmethod
    def database(client):
        return Database("aurora", cluster_arn="cluster-test", secret_arn="secret-test", database="ledger-test", client=client)

    def test_all_statements_and_commit_use_one_explicit_transaction(self):
        client = FakeDataAPI()
        database = self.database(client)
        with database.transaction() as tx:
            tx.execute("UPDATE accounts SET balance_cents=:balance WHERE id=:id", {"balance": 123, "id": "account-1"})
            row = tx.one("SELECT id,balance_cents FROM accounts WHERE id=:id", {"id": "account-1"})
        self.assertEqual(row, {"id": "account-1", "balance_cents": 123})
        self.assertEqual(client.calls[0], ("begin", {"resourceArn": "cluster-test", "secretArn": "secret-test", "database": "ledger-test"}))
        for kind, kwargs in client.calls[1:]:
            with self.subTest(kind=kind):
                self.assertEqual(kwargs["transactionId"], "transaction-123")
                self.assertEqual(kwargs["resourceArn"], "cluster-test")
                self.assertEqual(kwargs["secretArn"], "secret-test")
        mutations = [kwargs for kind, kwargs in client.calls if kind == "execute" and kwargs["sql"].startswith("UPDATE")]
        self.assertEqual(len(mutations), 1)
        parameters = {item["name"]: item["value"] for item in mutations[0]["parameters"]}
        self.assertEqual(parameters, {"balance": {"longValue": 123}, "id": {"stringValue": "account-1"}})
        self.assertEqual([kind for kind, _ in client.calls].count("commit"), 1)
        self.assertNotIn("rollback", [kind for kind, _ in client.calls])

    def test_sql_failure_rolls_back_same_transaction_without_statement_retry(self):
        client = FakeDataAPI(fail_statement=True)
        with self.assertRaises(RuntimeError) as raised:
            with self.database(client).transaction() as tx:
                tx.execute("UPDATE accounts SET balance_cents=:balance", {"balance": 1})
        self.assertIs(raised.exception, client.sql_failure)
        self.assertNotIn("commit", [kind for kind, _ in client.calls])
        rollbacks = [kwargs for kind, kwargs in client.calls if kind == "rollback"]
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(rollbacks[0]["transactionId"], "transaction-123")
        updates = [kwargs for kind, kwargs in client.calls if kind == "execute" and kwargs["sql"].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)

    def test_rollback_failure_does_not_mask_the_original_sql_failure(self):
        client = FakeDataAPI(fail_statement=True, fail_rollback=True)
        with self.assertRaises(RuntimeError) as raised:
            with self.database(client).transaction() as tx:
                tx.execute("UPDATE accounts SET balance_cents=1")
        self.assertIs(raised.exception, client.sql_failure)

    @unittest.skipUnless(importlib.util.find_spec("boto3") is not None, "optional boto3 is not installed")
    def test_sdk_does_not_retry_individual_sql_statements(self):
        client = FakeDataAPI()
        with patch("boto3.client", return_value=client) as constructor:
            database = Database("aurora", cluster_arn="cluster-test", secret_arn="secret-test")
        self.assertIs(database.client, client)
        self.assertEqual(constructor.call_args.args, ("rds-data",))
        config = constructor.call_args.kwargs["config"]
        self.assertEqual(config.retries["total_max_attempts"], 1)
        self.assertLessEqual(config.connect_timeout, 5)
        self.assertLessEqual(config.read_timeout, 15)


class LambdaBoundaryTests(unittest.TestCase):
    def setUp(self):
        handlers.service.cache_clear()
        self.addCleanup(handlers.service.cache_clear)

    @staticmethod
    def event(path="/api/accounts", method="GET", authorizer=None, headers=None, body=None):
        context = {"http": {"method": method}}
        if authorizer is not None:
            context["authorizer"] = authorizer
        return {"rawPath": path, "rawQueryString": "", "requestContext": context,
                "headers": headers or {}, "body": json.dumps(body or {})}

    @staticmethod
    def verified(sub="verified-owner", scopes=True):
        claims = {"sub": sub}
        if scopes:
            claims["scope"] = "openid ledger-api/access"
        return {"jwt": {"claims": claims}}

    def test_missing_verified_jwt_context_is_rejected_before_database_access(self):
        with patch.object(handlers, "service") as service:
            response = handlers.accounts_handler(self.event(), None)
        self.assertEqual(response["statusCode"], 401)
        self.assertEqual(json.loads(response["body"])["error"]["code"], "unauthorized")
        service.assert_not_called()

    def test_verified_subject_without_required_scope_is_rejected(self):
        with patch.object(handlers, "service") as service:
            response = handlers.accounts_handler(self.event(authorizer=self.verified(scopes=False)), None)
        self.assertEqual(response["statusCode"], 401)
        service.assert_not_called()

    def test_forged_header_alone_cannot_supply_a_customer_identity(self):
        forged = "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJhdHRhY2tlciIsInNjb3BlIjoibGVkZ2VyLWFwaS9hY2Nlc3MifQ."
        with patch.object(handlers, "service") as service:
            response = handlers.accounts_handler(self.event(headers={"Authorization": forged}), None)
        self.assertEqual(response["statusCode"], 401)
        service.assert_not_called()

    def test_local_debug_routes_are_not_exposed_even_with_verified_identity(self):
        for path in ("/api/dev/login", "/api/dev/worker", "/api/dev/sync", "/api/reconciliation"):
            for handler in (handlers.accounts_handler, handlers.transactions_handler):
                with self.subTest(path=path, handler=handler.__name__), patch.object(handlers, "service") as service:
                    response = handler(self.event(path, "POST" if path != "/api/reconciliation" else "GET", self.verified()), None)
                    self.assertEqual(response["statusCode"], 404)
                    service.assert_not_called()

    def test_authorized_account_request_uses_verified_subject_not_header_claims(self):
        ledger = Mock()
        ledger.list_accounts.return_value = [{"id": "owned-account", "balance_cents": 123}]
        with patch.object(handlers, "service", return_value=ledger):
            response = handlers.accounts_handler(self.event(authorizer=self.verified("verified-owner"), headers={"Authorization": "Bearer untrusted-client-value"}), None)
        self.assertEqual(response["statusCode"], 200)
        ledger.list_accounts.assert_called_once_with("verified-owner")
        self.assertEqual(json.loads(response["body"])["accounts"][0]["id"], "owned-account")

    def test_null_gateway_scopes_use_claim_scope_and_still_fail_closed(self):
        for allowed in (True, False):
            authorizer = self.verified(scopes=allowed)
            authorizer['jwt']['scopes'] = None
            ledger = Mock()
            ledger.list_accounts.return_value = []
            with self.subTest(allowed=allowed), patch.object(handlers, 'service', return_value=ledger) as service:
                result = handlers.accounts_handler(self.event(authorizer=authorizer), None)
            self.assertEqual(result['statusCode'], 200 if allowed else 401)
            if not allowed:
                service.assert_not_called()

    def test_customer_and_transaction_handlers_reject_each_others_routes(self):
        cases = ((handlers.accounts_handler, "/api/transfers", "POST"),
                 (handlers.transactions_handler, "/api/accounts", "GET"))
        for handler, path, method in cases:
            with self.subTest(path=path), patch.object(handlers, "service") as service:
                response = handler(self.event(path, method, self.verified()), None)
                self.assertEqual(response["statusCode"], 404)
                service.assert_not_called()

    def test_authentication_configuration_fails_closed(self):
        cases = ({"DB_BACKEND": "aurora", "AUTH_MODE": "local"},
                 {"DB_BACKEND": "sqlite", "AUTH_MODE": "cognito"}, {})
        for values in cases:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True), patch.object(handlers, "Database") as database:
                handlers.service.cache_clear()
                with self.assertRaises(RuntimeError):
                    handlers.service()
                database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
