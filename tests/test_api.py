"""API client contract tests using a fake session; no live requests are made."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/domres"
PACKAGE_NAME = "_domres_test_package"
if PACKAGE_NAME not in sys.modules:
    _test_package = types.ModuleType(PACKAGE_NAME)
    _test_package.__path__ = [str(COMPONENT)]
    sys.modules[PACKAGE_NAME] = _test_package


def _load_module(name: str):
    qualified_name = f"{PACKAGE_NAME}.{name}"
    if qualified_name in sys.modules:
        return sys.modules[qualified_name]
    spec = importlib.util.spec_from_file_location(qualified_name, COMPONENT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module


_load_module("const")
_load_module("data")

# Home Assistant supplies aiohttp at runtime. Use a tiny stand-in here so these
# contract tests remain runnable with the Python standard library alone.
_fake_aiohttp = types.ModuleType("aiohttp")
_fake_aiohttp.ClientError = type("ClientError", (Exception,), {})
_fake_aiohttp.ClientTimeout = lambda **kwargs: kwargs
_fake_aiohttp.ClientSession = type("ClientSession", (), {})
_aiohttp_module = sys.modules.setdefault("aiohttp", _fake_aiohttp)
_api_module = _load_module("api")
DomResApi = _api_module.DomResApi
DomResApiError = _api_module.DomResApiError
DomResCannotConnect = _api_module.DomResCannotConnect


class FakeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self) -> str:
        return json.dumps(self._body)


class FakeSession:
    def __init__(self, get_bodies: list[object] | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.get_bodies = get_bodies or [
            {"balance": {"value": 0}, "fine": {"value": 0}},
            {"indicationAvailability": True, "days": "19", "title": "Период"},
            {
                "status": "NOT_DECLARED",
                "dateDeclared": None,
                "datePeriodFrom": "01.11.2026",
                "datePeriodTo": "25.11.2026",
            },
            [],
        ]

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(200, {"auth": "test-token"})

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if method != "GET":
            raise AssertionError(f"Unexpected non-read request: {method} {url}")
        return FakeResponse(200, self.get_bodies.pop(0))


class ApiReadOnlyTests(unittest.TestCase):
    def test_auth_is_the_only_post_and_customer_header_is_configurable(self) -> None:
        async def run() -> tuple[FakeSession, dict]:
            session = FakeSession()
            api = DomResApi(session, "owner@example.org", "not-a-real-password", "tenant-test")
            await api.async_login()
            snapshot = await api.async_get_account_snapshot({"id": "account/1"})
            return session, snapshot

        session, snapshot = asyncio.run(run())
        methods = [call[0] for call in session.calls]
        self.assertEqual(methods, ["POST", "GET", "GET", "GET", "GET"])
        auth_call = session.calls[0]
        self.assertEqual(auth_call[2]["json"]["type"], "EMAIL")
        self.assertEqual(auth_call[2]["headers"]["Customer"], "tenant-test")
        self.assertEqual(snapshot["balance"]["balance"]["value"], 0)
        self.assertEqual(snapshot["availability"]["days"], "19")
        self.assertEqual(snapshot["indication_status"]["datePeriodTo"], "25.11.2026")
        self.assertEqual(snapshot["account_id"], "account/1")
        self.assertTrue(
            any("/v1/accounts/account%2F1/indications/status" in call[1] for call in session.calls)
        )
        self.assertTrue(all("/reading" not in call[1] for call in session.calls[1:]))

    def test_customer_header_is_omitted_when_unconfirmed(self) -> None:
        async def run() -> FakeSession:
            session = FakeSession()
            api = DomResApi(session, "+7 999 123-45-67", "not-a-real-password")
            await api.async_login()
            return session

        session = asyncio.run(run())
        method, _, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["json"]["type"], "PHONE")
        self.assertNotIn("Customer", kwargs["headers"])

    def test_account_validation_calls_real_api_methods(self) -> None:
        async def run() -> tuple[FakeSession, list[dict]]:
            session = FakeSession(get_bodies=[[{"id": "account-42"}]])
            api = DomResApi(session, "user@example.org", "not-a-real-password")
            await api.async_login()
            accounts = await api.async_get_accounts()
            return session, accounts

        session, accounts = asyncio.run(run())
        self.assertEqual(accounts, [{"id": "account-42"}])
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])

    def test_empty_primary_account_list_is_preserved(self) -> None:
        async def run() -> list[dict]:
            session = FakeSession(get_bodies=[{"accounts": [], "data": [{"id": "fallback"}]}])
            api = DomResApi(session, "user@example.org", "not-a-real-password")
            await api.async_login()
            return await api.async_get_accounts()

        self.assertEqual(asyncio.run(run()), [])

    def test_falsey_account_id_is_preserved(self) -> None:
        async def run() -> tuple[FakeSession, dict]:
            session = FakeSession()
            api = DomResApi(session, "user@example.org", "not-a-real-password")
            await api.async_login()
            snapshot = await api.async_get_account_snapshot(
                {"id": 0, "accountId": "should-not-replace-zero"}
            )
            return session, snapshot

        session, snapshot = asyncio.run(run())
        self.assertEqual(snapshot["account_id"], "0")
        self.assertTrue(any("/accounts/0/" in call[1] for call in session.calls))

    def test_all_failed_read_endpoints_raise_instead_of_reporting_success(self) -> None:
        class FailedReadSession(FakeSession):
            def request(self, method: str, url: str, **kwargs):
                self.calls.append((method, url, kwargs))
                return FakeResponse(503, {"message": "unavailable"})

        async def run() -> None:
            api = DomResApi(FailedReadSession(), "user@example.org", "not-a-real-password")
            await api.async_login()
            await api.async_get_account_snapshot({"id": "account-1"})

        with self.assertRaises(DomResApiError):
            asyncio.run(run())

    def test_network_failure_propagates_for_coordinator_retry(self) -> None:
        class OfflineSession(FakeSession):
            def request(self, method: str, url: str, **kwargs):
                self.calls.append((method, url, kwargs))
                raise _aiohttp_module.ClientError("offline")

        async def run() -> None:
            api = DomResApi(OfflineSession(), "user@example.org", "not-a-real-password")
            await api.async_login()
            await api.async_get_account_snapshot({"id": "account-1"})

        with self.assertRaises(DomResCannotConnect):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
