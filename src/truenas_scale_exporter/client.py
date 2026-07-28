"""JSON-RPC 2.0 over WebSocket client for the TrueNAS middleware.

TrueNAS deprecated its REST API in 25.04 and removes it entirely in 26.04
(Halfmoon); the JSON-RPC 2.0 over WebSocket API is the only forward-compatible
transport.  This client speaks that API and nothing else, so an exporter built
on it survives the upgrade that breaks every REST-based integration.
"""

from __future__ import annotations

import itertools
import json
import logging
import ssl
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

log = logging.getLogger(__name__)

# 25.04 introduced the versioned endpoint. `/api/current` always resolves to the
# newest version the appliance supports, which is what we want: a pinned
# `/api/v25.10` would 404 the moment the appliance is upgraded.
DEFAULT_API_PATH = "/api/current"


class TrueNASError(RuntimeError):
    """A middleware call failed, or the connection could not be established."""


def build_ws_url(url: str, api_path: str = DEFAULT_API_PATH) -> str:
    """Normalise a user-supplied host or URL into a websocket URL.

    Accepts ``truenas.example.com``, ``https://truenas.example.com`` or a full
    ``wss://truenas.example.com/api/current`` and always returns the last form.
    """
    if "://" not in url:
        url = f"wss://{url}"

    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    if scheme not in ("ws", "wss"):
        raise ValueError(f"unsupported scheme in {url!r}: {parts.scheme!r}")

    path = parts.path if parts.path not in ("", "/") else api_path
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class TrueNASClient:
    """A reconnecting, thread-safe JSON-RPC client.

    One connection is held open across scrapes rather than reconnecting each
    time: the middleware authenticates per-connection, so reconnecting per
    scrape would mean re-authenticating per scrape.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 15.0,
        api_path: str = DEFAULT_API_PATH,
    ) -> None:
        self._url = build_ws_url(url, api_path)
        self._api_key = api_key
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._ws = None

        if verify_tls:
            self._ssl_context: ssl.SSLContext | None = ssl.create_default_context()
        else:
            # TrueNAS ships a self-signed certificate by default. Opting out is
            # a deliberate, documented choice, not the default.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx

    @property
    def url(self) -> str:
        return self._url

    # -- connection handling -------------------------------------------------

    def _connect(self) -> None:
        kwargs: dict[str, Any] = {"open_timeout": self._timeout}
        if self._url.startswith("wss://"):
            kwargs["ssl"] = self._ssl_context

        log.debug("connecting to %s", self._url)
        self._ws = connect(self._url, **kwargs)

        try:
            self._authenticate()
        except Exception:
            self.close()
            raise

    def _authenticate(self) -> None:
        ok = self._raw_call("auth.login_with_api_key", [self._api_key])
        if ok is not True:
            raise TrueNASError(
                "authentication rejected — check the API key is valid and "
                "belongs to a user that still exists"
            )
        log.debug("authenticated against %s", self._url)

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass

    # -- calls ---------------------------------------------------------------

    def _raw_call(self, method: str, params: list[Any]) -> Any:
        """Issue one JSON-RPC call on an already-established connection."""
        assert self._ws is not None
        request_id = next(self._ids)
        self._ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        )

        # The middleware also pushes unsolicited notifications (collection
        # updates, job progress). Those carry no "id", so skip anything that
        # is not the response we are waiting for.
        while True:
            raw = self._ws.recv(timeout=self._timeout)
            message = json.loads(raw)

            if message.get("id") != request_id:
                continue

            if "error" in message:
                err = message["error"]
                raise TrueNASError(f"{method} failed: {err.get('message', err)}")
            return message.get("result")

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Call a middleware method, reconnecting once if the socket is stale.

        A long-lived connection can be closed by a middleware restart or a
        TrueNAS upgrade. Retrying once turns that into a single failed scrape
        instead of a permanently dead exporter.
        """
        params = params or []
        with self._lock:
            for attempt in (1, 2):
                if self._ws is None:
                    self._connect()
                try:
                    return self._raw_call(method, params)
                except (WebSocketException, OSError, json.JSONDecodeError) as exc:
                    self.close()
                    if attempt == 2:
                        raise TrueNASError(
                            f"{method} failed after reconnect: {exc}"
                        ) from exc
                    log.debug("connection lost during %s, reconnecting", method)
        raise AssertionError("unreachable")
