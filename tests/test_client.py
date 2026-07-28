"""Protocol tests for the JSON-RPC client.

These run against a **real websocket server** on localhost rather than patching
``websockets.sync.client.connect``.

That distinction matters. Patching the library out would test this code against
our own assumptions about how the library behaves, which is precisely the thing
most likely to be wrong — the `ssl` / `ssl_context` keyword rename is exactly
the class of bug a mock would sail straight past. A local server exercises the
real handshake, the real framing and the real close semantics, so a breaking
change in `websockets` fails here instead of in production.

What these still cannot prove is that TrueNAS replies the way we assume. Only
running against an appliance does that; see `--once`.
"""

from __future__ import annotations

import json
import ssl
import threading

import pytest
from websockets.sync.server import serve

from truenas_scale_exporter.client import TrueNASClient, TrueNASError


class FakeMiddleware:
    """A minimal JSON-RPC 2.0 server that behaves like the TrueNAS middleware."""

    def __init__(self, *, auth_result=True, responses=None, notifications=0):
        self.auth_result = auth_result
        self.responses = responses or {}
        self.notifications = notifications
        self.received: list[str] = []
        self.connections = 0
        self.drop_next_connection = False
        self._server = None
        self._thread = None

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self):
        self._server = serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/current"

    # -- handler -------------------------------------------------------------

    def _handle(self, websocket):
        self.connections += 1

        for raw in websocket:
            request = json.loads(raw)
            method = request["method"]
            self.received.append(method)

            # Checked per message, not per connection: the test arms this flag
            # while the connection is already open, which is exactly how a
            # middleware restart presents to a long-lived client.
            if self.drop_next_connection and method != "auth.login_with_api_key":
                self.drop_next_connection = False
                websocket.close()
                return

            # Unsolicited notifications carry no "id"; the client must skip
            # them rather than mistaking one for its response.
            for _ in range(self.notifications):
                websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "collection_update",
                            "params": {"noise": True},
                        }
                    )
                )

            websocket.send(json.dumps(self._response(request, method)))

    def _response(self, request, method):
        if method == "auth.login_with_api_key":
            return {"jsonrpc": "2.0", "id": request["id"], "result": self.auth_result}

        value = self.responses.get(method, [])
        if isinstance(value, dict) and "error" in value:
            return {"jsonrpc": "2.0", "id": request["id"], "error": value["error"]}
        return {"jsonrpc": "2.0", "id": request["id"], "result": value}


def client_for(server: FakeMiddleware, **kwargs) -> TrueNASClient:
    return TrueNASClient(server.url, "test-key", timeout=5, **kwargs)


# -- happy path --------------------------------------------------------------


def test_call_authenticates_then_returns_the_result():
    with FakeMiddleware(responses={"pool.query": [{"name": "tank"}]}) as server:
        client = client_for(server)
        try:
            assert client.call("pool.query") == [{"name": "tank"}]
        finally:
            client.close()

        # Authentication happens once, before the first real call.
        assert server.received == ["auth.login_with_api_key", "pool.query"]


def test_authentication_happens_once_per_connection_not_per_call():
    """The connection is deliberately long-lived; re-authenticating each scrape
    would triple the middleware round-trips for no benefit."""
    with FakeMiddleware(responses={"system.info": {"version": "25.10.5"}}) as server:
        client = client_for(server)
        try:
            client.call("system.info")
            client.call("system.info")
            client.call("system.info")
        finally:
            client.close()

        assert server.received.count("auth.login_with_api_key") == 1
        assert server.received.count("system.info") == 3
        assert server.connections == 1


# -- protocol edge cases -----------------------------------------------------


def test_unsolicited_notifications_are_skipped():
    """The middleware pushes collection updates and job progress unprompted."""
    with FakeMiddleware(
        responses={"pool.query": [{"name": "tank"}]}, notifications=3
    ) as server:
        client = client_for(server)
        try:
            assert client.call("pool.query") == [{"name": "tank"}]
        finally:
            client.close()


def test_middleware_error_becomes_a_truenas_error():
    error = {"error": {"code": -32000, "message": "Not authorized"}}
    with FakeMiddleware(responses={"pool.query": error}) as server:
        client = client_for(server)
        try:
            with pytest.raises(TrueNASError, match="Not authorized"):
                client.call("pool.query")
        finally:
            client.close()


def test_rejected_authentication_is_reported_clearly():
    with FakeMiddleware(auth_result=False) as server:
        client = client_for(server)
        try:
            with pytest.raises(TrueNASError, match="authentication rejected"):
                client.call("pool.query")
        finally:
            client.close()


def test_a_dropped_connection_is_retried_once_and_succeeds():
    """A middleware restart or an appliance upgrade closes the socket. That
    should cost one failed call at most, not leave a permanently dead exporter."""
    with FakeMiddleware(responses={"pool.query": [{"name": "tank"}]}) as server:
        client = client_for(server)
        try:
            client.call("pool.query")  # establishes the connection
            server.drop_next_connection = True
            assert client.call("pool.query") == [{"name": "tank"}]
        finally:
            client.close()

        # Reconnected, and re-authenticated on the new connection.
        assert server.connections == 2
        assert server.received.count("auth.login_with_api_key") == 2


def test_calls_fail_cleanly_when_the_server_is_unreachable():
    with FakeMiddleware() as server:
        url = server.url
    # Server is now shut down; the port should refuse the connection.
    client = TrueNASClient(url, "test-key", timeout=3)
    with pytest.raises((TrueNASError, OSError)):
        client.call("pool.query")
    client.close()


def test_close_is_safe_to_call_repeatedly():
    with FakeMiddleware(responses={"system.info": {}}) as server:
        client = client_for(server)
        client.call("system.info")
        client.close()
        client.close()  # must not raise


# -- TLS -------------------------------------------------------------------


def test_tls_verification_is_on_by_default():
    """The permissive path must be opt-in. `--insecure` exposes the API key to
    an active man-in-the-middle, so it can never be the default."""
    client = TrueNASClient("nas.example.com", "k")
    context = client._ssl_context  # pylint: disable=protected-access
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED


def test_insecure_disables_verification_exactly_as_advertised():
    client = TrueNASClient("nas.example.com", "k", verify_tls=False)
    context = client._ssl_context  # pylint: disable=protected-access
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_NONE


def test_url_property_exposes_the_normalised_target():
    client = TrueNASClient("nas.example.com", "k")
    assert client.url == "wss://nas.example.com/api/current"


def test_the_api_key_is_sent_as_the_login_parameter():
    captured = {}

    class CapturingMiddleware(FakeMiddleware):
        def _response(self, request, method):
            if method == "auth.login_with_api_key":
                captured["params"] = request["params"]
            return super()._response(request, method)

    with CapturingMiddleware(responses={"system.info": {}}) as server:
        client = TrueNASClient(server.url, "secret-key-value", timeout=5)
        try:
            client.call("system.info")
        finally:
            client.close()

    assert captured["params"] == ["secret-key-value"]
