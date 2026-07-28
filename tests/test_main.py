"""Tests for argument handling, key resolution and the WSGI surface.

Key resolution is worth testing carefully: getting the precedence wrong means
an operator who mounts a Secret file and also has a stale environment variable
silently authenticates with the wrong credential, which presents as a
permissions problem rather than a configuration one.
"""

from __future__ import annotations

import io

import pytest
from prometheus_client import CollectorRegistry, Gauge

from truenas_scale_exporter import __version__
from truenas_scale_exporter.__main__ import _read_api_key, build_app, parse_args

ENV_VARS = (
    "TRUENAS_URL",
    "TRUENAS_API_KEY",
    "TRUENAS_API_KEY_FILE",
    "TRUENAS_INSECURE",
    "TRUENAS_TIMEOUT",
    "TRUENAS_EXPORTER_PORT",
    "TRUENAS_EXPORTER_LISTEN_ADDRESS",
    "TRUENAS_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The parser reads os.environ at call time, so a stray variable in the
    developer's shell would otherwise change what these tests assert."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# -- key resolution ----------------------------------------------------------


def test_api_key_file_flag_wins_over_everything(tmp_path, monkeypatch):
    key_file = tmp_path / "api-key"
    key_file.write_text("from-flag-file")
    other = tmp_path / "other"
    other.write_text("from-env-file")

    monkeypatch.setenv("TRUENAS_API_KEY_FILE", str(other))
    monkeypatch.setenv("TRUENAS_API_KEY", "from-env")

    args = parse_args(["--api-key-file", str(key_file), "--api-key", "from-flag"])
    assert _read_api_key(args) == "from-flag-file"


def test_env_key_file_beats_inline_values(tmp_path, monkeypatch):
    key_file = tmp_path / "api-key"
    key_file.write_text("from-env-file")
    monkeypatch.setenv("TRUENAS_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("TRUENAS_API_KEY", "from-env")

    args = parse_args(["--api-key", "from-flag"])
    assert _read_api_key(args) == "from-env-file"


def test_inline_flag_beats_environment():
    args = parse_args(["--api-key", "from-flag"])
    assert _read_api_key(args) == "from-flag"


def test_environment_is_the_last_resort(monkeypatch):
    monkeypatch.setenv("TRUENAS_API_KEY", "from-env")
    assert _read_api_key(parse_args([])) == "from-env"


def test_surrounding_whitespace_is_stripped(tmp_path):
    """A key file written with `echo` gains a trailing newline, which the
    middleware would reject as an invalid key."""
    key_file = tmp_path / "api-key"
    key_file.write_text("  padded-key\n")
    args = parse_args(["--api-key-file", str(key_file)])
    assert _read_api_key(args) == "padded-key"


def test_missing_key_exits_with_a_useful_message():
    with pytest.raises(SystemExit) as excinfo:
        _read_api_key(parse_args([]))
    assert "TRUENAS_API_KEY_FILE" in str(excinfo.value)


# -- argument parsing --------------------------------------------------------


def test_defaults():
    args = parse_args([])
    assert args.port == 9819
    assert args.listen_address == "0.0.0.0"
    assert args.insecure is False
    assert args.timeout == 15.0


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("yes", True)])
def test_insecure_can_be_set_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("TRUENAS_INSECURE", value)
    assert parse_args([]).insecure is expected


def test_insecure_defaults_off_for_unrecognised_values(monkeypatch):
    """Anything ambiguous must fail safe — TLS verification stays on."""
    monkeypatch.setenv("TRUENAS_INSECURE", "maybe")
    assert parse_args([]).insecure is False


def test_kubernetes_service_link_variable_does_not_kill_startup(monkeypatch, caplog):
    """Regression: a Service named `truenas-exporter` crash-looped the pod.

    The kubelet injects a legacy Docker-link variable for every Service in the
    namespace, named after the Service with dashes uppercased to underscores.
    A Service called `truenas-exporter` therefore produces
    TRUENAS_EXPORTER_PORT=tcp://10.97.147.236:9819 — colliding exactly with
    this exporter's own port setting, and `int()` on it raised ValueError
    before argparse ever ran.

    The chart now sets enableServiceLinks: false, but the binary must not
    depend on that: raw manifests and Compose users can still hit it.
    """
    monkeypatch.setenv("TRUENAS_EXPORTER_PORT", "tcp://10.97.147.236:9819")

    with caplog.at_level("WARNING"):
        args = parse_args([])

    assert args.port == 9819, "must fall back to the default rather than crash"
    assert "enableServiceLinks" in caplog.text, (
        "the warning should name the fix, since the cause is not obvious "
        "from a ValueError about a URL"
    )


def test_a_non_numeric_timeout_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("TRUENAS_TIMEOUT", "not-a-number")
    assert parse_args([]).timeout == 15.0


def test_an_empty_numeric_env_var_uses_the_default(monkeypatch):
    """An unset-but-present variable is common in Compose and Helm templating."""
    monkeypatch.setenv("TRUENAS_EXPORTER_PORT", "")
    assert parse_args([]).port == 9819


def test_url_and_port_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("TRUENAS_URL", "nas.example.com")
    monkeypatch.setenv("TRUENAS_EXPORTER_PORT", "9999")
    args = parse_args([])
    assert args.url == "nas.example.com"
    assert args.port == 9999


# -- WSGI surface ------------------------------------------------------------


def call_app(app, path):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(
        app(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": "GET",
                "wsgi.input": io.BytesIO(b""),
            },
            start_response,
        )
    )
    return captured["status"], body


@pytest.fixture
def app():
    registry = CollectorRegistry()
    Gauge("example_metric", "An example.", registry=registry).set(42)
    return build_app(registry)


def test_metrics_endpoint_serves_the_registry(app):
    status, body = call_app(app, "/metrics")
    assert status.startswith("200")
    assert b"example_metric 42.0" in body


def test_health_endpoint(app):
    status, body = call_app(app, "/health")
    assert status.startswith("200")
    assert body == b"ok\n"


def test_landing_page_names_the_version(app):
    status, body = call_app(app, "/")
    assert status.startswith("200")
    assert __version__.encode() in body


def test_unknown_path_is_a_404(app):
    status, _ = call_app(app, "/nope")
    assert status.startswith("404")
