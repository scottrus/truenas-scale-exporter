"""Command-line entrypoint for truenas-scale-exporter."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import TypeVar
from wsgiref.simple_server import WSGIRequestHandler, make_server

from prometheus_client import CollectorRegistry, make_wsgi_app
from prometheus_client.exposition import generate_latest

from . import __version__
from .client import TrueNASClient, TrueNASError
from .collector import TrueNASCollector

log = logging.getLogger("truenas_scale_exporter")

T = TypeVar("T", int, float)

DEFAULT_PORT = 9819


class _QuietHandler(WSGIRequestHandler):
    """Suppress per-request stderr logging; a scrape every 30s is not news."""

    # The parameter name is fixed by BaseHTTPRequestHandler's signature; it
    # shadows a builtin, but renaming it would be an incompatible override.
    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        pass


def _numeric_env(name: str, default: T, cast) -> T:
    """Read a numeric environment variable, tolerating a non-numeric value.

    This exists because of a specific Kubernetes footgun. The kubelet injects a
    legacy Docker-link variable for every Service in the pod's namespace, named
    after the Service with dashes uppercased to underscores:

        Service "truenas-exporter"  ->  TRUENAS_EXPORTER_PORT=tcp://10.0.0.1:9819

    That collides exactly with this exporter's own port setting, so naming the
    release `truenas-exporter` — the obvious name — made the container die at
    startup with a ValueError about a URL. The chart now sets
    `enableServiceLinks: false`, but an operator on an older chart, a raw
    manifest, or plain Docker Compose can still hit it.

    Falling back to the default with a loud warning beats crash-looping on a
    value the operator never set.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log.warning(
            "Ignoring %s=%r — not a number; using %r instead. In Kubernetes "
            "this is usually the service-link variable the kubelet injects "
            "for a Service of the same name; set enableServiceLinks: false "
            "on the pod spec.",
            name,
            raw,
            default,
        )
        return default


def _read_api_key(args: argparse.Namespace) -> str:
    """Resolve the API key, preferring file-based sources.

    A file is preferred because it is how Kubernetes and Docker secrets are
    delivered, and because it keeps the key out of the process environment and
    the process table.
    """
    if args.api_key_file:
        return Path(args.api_key_file).read_text(encoding="utf-8").strip()

    env_file = os.environ.get("TRUENAS_API_KEY_FILE")
    if env_file:
        return Path(env_file).read_text(encoding="utf-8").strip()

    key = args.api_key or os.environ.get("TRUENAS_API_KEY")
    if not key:
        raise SystemExit(
            "No API key supplied. Set TRUENAS_API_KEY_FILE (preferred), "
            "TRUENAS_API_KEY, or pass --api-key-file / --api-key."
        )
    return key.strip()


def build_app(registry: CollectorRegistry):
    """WSGI app serving /metrics, plus /health and a landing page."""
    metrics_app = make_wsgi_app(registry)

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/metrics":
            return metrics_app(environ, start_response)

        if path == "/health":
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok\n"]

        if path == "/":
            body = (
                f"truenas-scale-exporter {__version__}\n"
                "Metrics: /metrics\nHealth: /health\n"
            ).encode()
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [body]

        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found\n"]

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="truenas-scale-exporter",
        description=(
            "Prometheus exporter for TrueNAS, speaking the JSON-RPC 2.0 "
            "WebSocket API (TrueNAS 25.04+)."
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("TRUENAS_URL"),
        help="TrueNAS host or URL, e.g. truenas.example.com (env: TRUENAS_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key. Prefer --api-key-file (env: TRUENAS_API_KEY)",
    )
    parser.add_argument(
        "--api-key-file",
        default=None,
        help="File containing the API key (env: TRUENAS_API_KEY_FILE)",
    )
    parser.add_argument(
        "--listen-address",
        default=os.environ.get("TRUENAS_EXPORTER_LISTEN_ADDRESS", "0.0.0.0"),
        help="Address to bind (env: TRUENAS_EXPORTER_LISTEN_ADDRESS)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_numeric_env("TRUENAS_EXPORTER_PORT", DEFAULT_PORT, int),
        help=f"Port to bind, default {DEFAULT_PORT} (env: TRUENAS_EXPORTER_PORT)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=os.environ.get("TRUENAS_INSECURE", "").lower() in ("1", "true", "yes"),
        help="Skip TLS verification. TrueNAS ships a self-signed certificate, "
        "so this is often needed (env: TRUENAS_INSECURE)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_numeric_env("TRUENAS_TIMEOUT", 15.0, float),
        help="Per-call timeout in seconds (env: TRUENAS_TIMEOUT)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("TRUENAS_LOG_LEVEL", "INFO"),
        help="Logging level (env: TRUENAS_LOG_LEVEL)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect once, print the exposition to stdout, and exit. Use "
        "this to verify connectivity before deploying.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.url:
        raise SystemExit("No TrueNAS URL supplied. Set TRUENAS_URL or --url.")

    api_key = _read_api_key(args)

    client = TrueNASClient(
        args.url,
        api_key,
        verify_tls=not args.insecure,
        timeout=args.timeout,
    )
    # The key is now held only inside the client; drop the local reference so
    # it does not linger in a frame that might end up in a traceback.
    del api_key

    registry = CollectorRegistry()
    registry.register(TrueNASCollector(client))

    if args.once:
        try:
            sys.stdout.write(generate_latest(registry).decode())
        except TrueNASError as exc:
            log.error("collection failed: %s", exc)
            return 1
        finally:
            client.close()
        return 0

    log.info(
        "truenas-scale-exporter %s serving on %s:%s for %s",
        __version__,
        args.listen_address,
        args.port,
        client.url,
    )

    httpd = make_server(
        args.listen_address,
        args.port,
        build_app(registry),
        handler_class=_QuietHandler,
    )

    def _shutdown(signum, _frame):
        # shutdown() blocks until serve_forever() returns, so it cannot be
        # called from the signal handler itself without deadlocking.
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        httpd.serve_forever()
    finally:
        client.close()
        httpd.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
