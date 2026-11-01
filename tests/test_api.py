"""HTTP integration tests against an isolated local server and real database file.

Run with the backend directory on PYTHONPATH:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]


class HTTPAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="capital-ledger-api-")
        cls.addClassCleanup(cls.cleanup)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cls.base_url = "http://127.0.0.1:" + str(port)
        cls.log_path = Path(cls.scratch.name) / "server.log"
        cls.log = cls.log_path.open("w", encoding="utf-8")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        # The free launcher must stay local even in a shell with cloud DB settings.
        env["DATABASE_URL"] = "postgresql://must-not-connect.example.invalid/paid-database"
        cls.process = subprocess.Popen([
            sys.executable, str(ROOT / "start-local.py"), "--host", "127.0.0.1",
            "--port", str(port), "--database", str(Path(cls.scratch.name) / "test.sqlite"),
        ], cwd=cls.scratch.name, env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        last_failure = "no response"
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                cls.log.flush()
                raise RuntimeError("Local server exited:\n" + cls.log_path.read_text(encoding="utf-8"))
            try:
                status, _, _ = cls.request("GET", "/api/health")
                if status == 200:
                    break
                last_failure = "HTTP " + str(status)
            except (URLError, ConnectionError, TimeoutError) as exc:
                last_failure = str(exc)
            time.sleep(0.05)
        else:
            cls.process.terminate()
            cls.process.wait(timeout=5)
            cls.log.flush()
            raise RuntimeError("Local server did not become ready (" + last_failure + "):\n" + cls.log_path.read_text(encoding="utf-8"))
        cls.alice = cls.login("alice")
        cls.bob = cls.login("bob")

    @classmethod
    def cleanup(cls):
        if getattr(cls, "process", None) is not None and cls.process.poll() is None:
            cls.process.terminate()
            cls.process.wait(timeout=5)
        if getattr(cls, "log", None) is not None:
            cls.log.close()
        if getattr(cls, "scratch", None) is not None:
            cls.scratch.cleanup()

    @classmethod
    def request(cls, method, path, payload=None, token=None, key=None, headers=None):
        request_headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        if key is not None:
            request_headers["Idempotency-Key"] = key
        request_headers.update(headers or {})
        request = Request(cls.base_url + path, data=body, headers=request_headers, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read().decode("utf-8")
            return response.code, json.loads(raw) if raw else {}, dict(response.headers.items())

    @classmethod
    def login(cls, user):
        status, result, _ = cls.request("POST", "/api/dev/login", {"user": user})
        if status != 200:
            raise RuntimeError("Development login failed: " + repr(result))
        token = result.get("token") or result.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Development login returned no token")
        return token

    @staticmethod
    def entity(result, name):
        return result.get(name, result)

    def create_account(self, token=None, balance=10000):
        status, result, _ = self.request("POST", "/api/accounts", {
            "name": "HTTP test " + uuid.uuid4().hex[:8],
            "full_name": "Fictional API Test User",
            "date_of_birth": "1995-01-01",
            "initial_balance_cents": balance,
        }, token or self.alice, uuid.uuid4().hex)
        self.assertIn(status, (200, 201), result)
        account = self.entity(result, "account")
        self.assertIn("id", account, result)
        return account

    def test_health_is_public_but_account_routes_require_authentication(self):
        status, health, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["mode"], "local")
        self.assertEqual(health["database"], "sqlite")
        for path in ("/api/me", "/api/accounts", "/api/transactions", "/api/reconciliation"):
            with self.subTest(path=path):
                status, result, _ = self.request("GET", path)
                self.assertEqual(status, 401, result)
        status, result, _ = self.request("POST", "/api/dev/worker", {"paused": True})
        self.assertEqual(status, 401, result)
        status, result, _ = self.request("POST", "/api/dev/sync", {})
        self.assertEqual(status, 401, result)

    def test_arbitrary_bearer_and_forged_jwt_are_rejected(self):
        forged = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhbGljZSJ9."
        for token in ("alice", "not-a-valid-token", forged):
            with self.subTest(token=token):
                status, result, _ = self.request("GET", "/api/accounts", token=token)
                self.assertEqual(status, 401, result)

    def test_another_user_cannot_read_or_mutate_an_account(self):
        account = self.create_account()
        path = "/api/accounts/" + account["id"]
        status, result, _ = self.request("GET", path, token=self.bob)
        self.assertEqual(status, 403, result)
        status, result, _ = self.request("POST", path + "/withdrawals", {"amount_cents": 100}, self.bob, uuid.uuid4().hex)
        self.assertEqual(status, 403, result)
        status, result, _ = self.request("GET", path, token=self.alice)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.entity(result, "account")["balance_cents"], 10000)

    def test_required_customer_deposit_and_withdrawal_flow(self):
        account = self.create_account(balance=10000)
        self.assertEqual(account["full_name"], "Fictional API Test User")
        self.assertEqual(account["date_of_birth"], "1995-01-01")
        path = "/api/accounts/" + account["id"]
        for operation, cents in (("deposits", 2500), ("withdrawals", 4000)):
            status, result, _ = self.request("POST", path + "/" + operation, {"amount_cents": cents, "description": "Required workflow"}, self.alice, uuid.uuid4().hex)
            self.assertIn(status, (200, 201), result)
        status, result, _ = self.request("GET", path, token=self.alice)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.entity(result, "account")["balance_cents"], 8500)
        status, result, _ = self.request("GET", "/api/transactions?account_id=" + account["id"], token=self.alice)
        self.assertEqual(status, 200, result)
        transactions = result.get("transactions", result) if isinstance(result, dict) else result
        self.assertGreaterEqual(len(transactions), 3)

    def test_positive_integer_amounts_are_required(self):
        account = self.create_account()
        path = "/api/accounts/" + account["id"] + "/deposits"
        for amount in (0, -1, True, 1.5, "100", None):
            with self.subTest(amount=amount):
                status, result, _ = self.request("POST", path, {"amount_cents": amount}, self.alice, uuid.uuid4().hex)
                self.assertEqual(status, 400, result)

    def test_mutation_requires_idempotency_key(self):
        account = self.create_account()
        status, result, _ = self.request("POST", "/api/accounts/" + account["id"] + "/deposits", {"amount_cents": 100}, self.alice)
        self.assertEqual(status, 400, result)

    def test_retry_is_single_operation_and_changed_payload_conflicts(self):
        account = self.create_account(balance=0)
        path = "/api/accounts/" + account["id"] + "/deposits"
        key = uuid.uuid4().hex
        payload = {"amount_cents": 100, "description": "Retry test"}
        status, original, _ = self.request("POST", path, payload, self.alice, key)
        self.assertIn(status, (200, 201), original)
        status, repeated, _ = self.request("POST", path, payload, self.alice, key)
        self.assertIn(status, (200, 201), repeated)
        self.assertEqual(original["transaction_id"], repeated["transaction_id"])
        status, result, _ = self.request("POST", path, {"amount_cents": 200, "description": "Retry test"}, self.alice, key)
        self.assertEqual(status, 409, result)
        status, result, _ = self.request("GET", "/api/accounts/" + account["id"], token=self.alice)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.entity(result, "account")["balance_cents"], 100)

    def test_overdraft_is_rejected_without_changing_balance(self):
        account = self.create_account(balance=100)
        path = "/api/accounts/" + account["id"]
        status, result, _ = self.request("POST", path + "/withdrawals", {"amount_cents": 101}, self.alice, uuid.uuid4().hex)
        self.assertEqual(status, 409, result)
        status, result, _ = self.request("GET", path, token=self.alice)
        self.assertEqual(self.entity(result, "account")["balance_cents"], 100)

    def test_transfer_from_another_users_account_is_rejected(self):
        source = self.create_account(token=self.alice, balance=100)
        destination = self.create_account(token=self.bob, balance=0)
        status, result, _ = self.request("POST", "/api/transfers", {
            "source_account_id": source["id"], "destination_account_id": destination["id"],
            "amount_cents": 50,
        }, self.bob, uuid.uuid4().hex)
        self.assertEqual(status, 403, result)

    def test_transfer_changes_both_balances(self):
        source = self.create_account(balance=100)
        destination = self.create_account(balance=0)
        status, result, _ = self.request("POST", "/api/transfers", {
            "source_account_id": source["id"], "destination_account_id": destination["id"],
            "amount_cents": 40, "description": "HTTP transfer test",
        }, self.alice, uuid.uuid4().hex)
        self.assertIn(status, (200, 201), result)
        for account_id, expected in ((source["id"], 60), (destination["id"], 40)):
            status, result, _ = self.request("GET", "/api/accounts/" + account_id, token=self.alice)
            self.assertEqual(status, 200, result)
            self.assertEqual(self.entity(result, "account")["balance_cents"], expected)

    def test_paused_delivery_reports_lag_then_recovers(self):
        status, result, _ = self.request("POST", "/api/dev/worker", {"paused": True}, self.alice)
        self.assertEqual(status, 200, result)
        try:
            account = self.create_account(balance=0)
            path = "/api/accounts/" + account["id"]
            status, result, _ = self.request("POST", path + "/deposits", {"amount_cents": 123}, self.alice, uuid.uuid4().hex)
            self.assertIn(status, (200, 201), result)
            status, result, _ = self.request("GET", path, token=self.alice)
            observed = self.entity(result, "account")
            self.assertEqual(observed["balance_cents"], 123)
            self.assertFalse(observed["summary_synced"])
            status, report, _ = self.request("GET", "/api/reconciliation", token=self.alice)
            self.assertEqual(status, 200, report)
            self.assertTrue(report["ok"], report)
            self.assertIn(account["id"], [row["account_id"] for row in report["projection_lag"]])
            status, result, _ = self.request("POST", "/api/dev/worker", {"paused": False}, self.alice)
            self.assertEqual(status, 200, result)
            status, result, _ = self.request("POST", "/api/dev/sync", {}, self.alice)
            self.assertEqual(status, 200, result)
            status, result, _ = self.request("GET", path, token=self.alice)
            observed = self.entity(result, "account")
            self.assertTrue(observed["summary_synced"], observed)
            self.assertEqual(observed["summary_balance_cents"], 123)
        finally:
            self.request("POST", "/api/dev/worker", {"paused": False}, self.alice)
            self.request("POST", "/api/dev/sync", {}, self.alice)

    def test_cors_does_not_allow_an_arbitrary_origin(self):
        origin = "https://attacker.example"
        status, _, headers = self.request("OPTIONS", "/api/accounts", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        })
        allowed = next((value for key, value in headers.items() if key.lower() == "access-control-allow-origin"), None)
        self.assertNotIn(allowed, (origin, "*"))


if __name__ == "__main__":
    unittest.main()
