"""Collector tests, built on the response shapes the middleware really returns."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from truenas_scale_exporter.client import TrueNASError, build_ws_url
from truenas_scale_exporter.collector import TrueNASCollector


class FakeClient:
    """Stands in for TrueNASClient, returning canned middleware responses."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.params: list[tuple[str, object]] = []

    def params_for(self, method: str):
        for name, params in self.params:
            if name == method:
                return params
        raise AssertionError(f"{method} was never called")

    def call(self, method: str, params=None):
        self.calls.append(method)
        self.params.append((method, params))
        if method not in self.responses:
            raise TrueNASError(f"no canned response for {method}")
        value = self.responses[method]
        if isinstance(value, Exception):
            raise value
        return value


def healthy_mirror_pool():
    return {
        "name": "fast",
        "status": "ONLINE",
        "status_code": "OK",
        "healthy": True,
        "warning": False,
        "size": 1992864825344,
        "allocated": 7197945856,
        "free": 1985666879488,
        "fragmentation": "4",
        "autotrim": {"value": "on"},
        "scan": {
            "function": "SCRUB",
            "state": "FINISHED",
            "end_time": {"$date": 1785252973000},
            "errors": 0,
            "percentage": 100.0,
        },
        "topology": {
            "data": [
                {
                    "name": "mirror-0",
                    "type": "MIRROR",
                    "status": "ONLINE",
                    "children": [
                        {
                            "disk": "nvme0n1",
                            "status": "ONLINE",
                            "stats": {
                                "read_errors": 0,
                                "write_errors": 0,
                                "checksum_errors": 0,
                            },
                        },
                        {
                            "disk": "nvme1n1",
                            "status": "ONLINE",
                            "stats": {
                                "read_errors": 0,
                                "write_errors": 0,
                                "checksum_errors": 0,
                            },
                        },
                    ],
                }
            ]
        },
    }


def suspended_stripe_pool():
    """A mirror that lost a leg and then suspended.

    This is the shape that matters most: the surviving device is a bare DISK
    vdev with no children, the pool reports errors, and naive "is it ONLINE"
    monitoring would have said nothing was wrong for the whole window in which
    the mirror had already become a single point of failure.
    """
    return {
        "name": "degraded",
        "status": "ONLINE",
        "status_code": "IO_FAILURE_CONTINUE",
        "healthy": False,
        "warning": True,
        "size": 1992864825344,
        "allocated": 7197945856,
        "free": 1985666879488,
        "fragmentation": "4",
        "autotrim": {"value": "off"},
        "scan": {
            "function": "RESILVER",
            "state": "FINISHED",
            "end_time": {"$date": 1785252973000},
            "errors": 0,
            "percentage": 99.99,
        },
        "topology": {
            "data": [
                {
                    "name": "nvme3n1p2",
                    "type": "DISK",
                    "status": "ONLINE",
                    "disk": "nvme3n1",
                    "children": [],
                    "stats": {
                        "read_errors": 250,
                        "write_errors": 30,
                        "checksum_errors": 0,
                    },
                }
            ]
        },
    }


def build_registry(responses) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(TrueNASCollector(FakeClient(responses)))
    return registry


def default_responses(pools):
    return {
        "pool.query": pools,
        "disk.query": [
            {
                "name": "nvme0n1",
                "serial": "SERIAL0001",
                "model": "EXAMPLE NVME",
                "type": "SSD",
            }
        ],
        "disk.temperatures": {"nvme0n1": 41, "nvme1n1": 43},
        "alert.list": [
            {"klass": "SMARTStat", "level": "CRITICAL", "dismissed": False},
            {"klass": "VolumeStatus", "level": "CRITICAL", "dismissed": True},
            {"klass": "SMARTStat", "level": "CRITICAL", "dismissed": False},
        ],
        "system.info": {
            "version": "25.10.5",
            "hostname": "truenas",
            "model": "Example CPU",
            "system_product": "Example Server",
            "uptime_seconds": 24446.8,
            "boottime": {"$date": 1785247624000},
            "physmem": 134709493760,
        },
    }


def sample_value(registry, name, labels):
    value = registry.get_sample_value(name, labels)
    assert value is not None, f"{name}{labels} was not emitted"
    return value


def test_healthy_pool_reports_online_state_set():
    registry = build_registry(default_responses([healthy_mirror_pool()]))

    assert (
        sample_value(
            registry, "truenas_pool_status", {"pool": "fast", "status": "ONLINE"}
        )
        == 1
    )
    # Every other state must be present and zero, so an alerting rule can use
    # `== 0` without a missing series masking a real problem.
    for state in ("DEGRADED", "FAULTED", "SUSPENDED", "REMOVED"):
        assert (
            sample_value(
                registry, "truenas_pool_status", {"pool": "fast", "status": state}
            )
            == 0
        )

    assert sample_value(registry, "truenas_pool_healthy", {"pool": "fast"}) == 1
    assert (
        sample_value(registry, "truenas_pool_autotrim_enabled", {"pool": "fast"}) == 1
    )
    assert sample_value(
        registry, "truenas_pool_fragmentation_ratio", {"pool": "fast"}
    ) == pytest.approx(0.04)


def test_mirror_reports_two_children():
    registry = build_registry(default_responses([healthy_mirror_pool()]))

    assert (
        sample_value(
            registry,
            "truenas_pool_vdev_children",
            {
                "pool": "fast",
                "vdev": "mirror-0",
                "vdev_type": "MIRROR",
                "category": "data",
            },
        )
        == 2
    )


def test_lost_redundancy_is_visible_while_pool_still_reads_online():
    """The trap: status is ONLINE but redundancy is gone and IO is failing."""
    registry = build_registry(default_responses([suspended_stripe_pool()]))

    # Status alone says everything is fine...
    assert (
        sample_value(
            registry, "truenas_pool_status", {"pool": "degraded", "status": "ONLINE"}
        )
        == 1
    )

    # ...but these three do not.
    assert sample_value(registry, "truenas_pool_healthy", {"pool": "degraded"}) == 0
    assert (
        sample_value(
            registry,
            "truenas_pool_status_code",
            {"pool": "degraded", "status_code": "IO_FAILURE_CONTINUE"},
        )
        == 1
    )
    assert (
        sample_value(
            registry,
            "truenas_pool_vdev_children",
            {
                "pool": "degraded",
                "vdev": "nvme3n1p2",
                "vdev_type": "DISK",
                "category": "data",
            },
        )
        == 1
    )


def test_device_error_counters_are_exported():
    registry = build_registry(default_responses([suspended_stripe_pool()]))
    labels = {"pool": "degraded", "vdev": "nvme3n1p2", "device": "nvme3n1"}

    assert (
        sample_value(registry, "truenas_pool_device_read_errors_total", labels) == 250
    )
    assert (
        sample_value(registry, "truenas_pool_device_write_errors_total", labels) == 30
    )
    assert (
        sample_value(registry, "truenas_pool_device_checksum_errors_total", labels) == 0
    )


def resilvered_but_never_scrubbed_pool():
    """A pool whose only scan on record is a resilver.

    Taken from a real appliance: a mirror leg was replaced, the pool resilvered,
    and ZFS reports exactly one scan per pool — so there is no SCRUB record at
    all. Alerting that aggregates across scan types reads this pool as "scanned
    minutes ago" and stays silent forever, on the pool most in need of a scrub.
    """
    pool = healthy_mirror_pool()
    pool["name"] = "resilvered"
    pool["scan"] = {
        "function": "RESILVER",
        "state": "FINISHED",
        "end_time": {"$date": 1785252973000},
        "errors": 0,
        "percentage": 100.0,
    }
    return pool


def test_a_resilver_does_not_produce_a_scrub_series():
    """The label that lets an alert rule tell the two apart must be present.

    This is the data contract behind `{function="SCRUB"}` in the alerting
    rules: if a resilver ever emitted a SCRUB-labelled timestamp, the
    never-scrubbed rule would go quiet without anything else changing.
    """
    registry = build_registry(default_responses([resilvered_but_never_scrubbed_pool()]))

    assert (
        registry.get_sample_value(
            "truenas_pool_scan_end_timestamp_seconds",
            {"pool": "resilvered", "function": "SCRUB"},
        )
        is None
    )
    assert (
        sample_value(
            registry,
            "truenas_pool_scan_end_timestamp_seconds",
            {"pool": "resilvered", "function": "RESILVER"},
        )
        == 1785252973.0
    )


def test_scrub_completion_is_exported_as_a_timestamp():
    registry = build_registry(default_responses([healthy_mirror_pool()]))

    assert (
        sample_value(
            registry,
            "truenas_pool_scan_end_timestamp_seconds",
            {"pool": "fast", "function": "SCRUB"},
        )
        == 1785252973.0
    )


def test_disk_temperature_carries_serial_when_inventory_is_available():
    registry = build_registry(default_responses([healthy_mirror_pool()]))

    assert (
        sample_value(
            registry,
            "truenas_disk_temperature_celsius",
            {
                "disk": "nvme0n1",
                "model": "EXAMPLE NVME",
                "serial": "SERIAL0001",
                "type": "SSD",
            },
        )
        == 41
    )
    # A disk missing from the inventory still reports its temperature.
    assert (
        sample_value(
            registry,
            "truenas_disk_temperature_celsius",
            {"disk": "nvme1n1", "model": "", "serial": "", "type": ""},
        )
        == 43
    )


def test_disk_temperatures_is_called_with_the_disk_names():
    """The middleware requires a name list; calling with no params fails."""
    client = FakeClient(default_responses([healthy_mirror_pool()]))
    registry = CollectorRegistry()
    registry.register(TrueNASCollector(client))
    generate_latest(registry)

    assert client.params_for("disk.temperatures") == [["nvme0n1"]]


def test_alerts_are_counted_by_class_and_level():
    registry = build_registry(default_responses([healthy_mirror_pool()]))

    assert (
        sample_value(
            registry,
            "truenas_alerts",
            {"klass": "SMARTStat", "level": "CRITICAL", "dismissed": "false"},
        )
        == 2
    )
    assert (
        sample_value(
            registry,
            "truenas_alerts",
            {"klass": "VolumeStatus", "level": "CRITICAL", "dismissed": "true"},
        )
        == 1
    )


def test_one_failing_collector_does_not_suppress_the_others():
    responses = default_responses([healthy_mirror_pool()])
    responses["alert.list"] = TrueNASError("alert API unavailable")
    registry = build_registry(responses)

    assert (
        sample_value(registry, "truenas_collector_success", {"collector": "alerts"})
        == 0
    )
    assert (
        sample_value(registry, "truenas_collector_success", {"collector": "pools"}) == 1
    )
    # Pool data is still present, and truenas_up correctly reports degraded.
    assert sample_value(registry, "truenas_pool_healthy", {"pool": "fast"}) == 1
    assert registry.get_sample_value("truenas_up") == 0


def test_exposition_renders():
    registry = build_registry(default_responses([healthy_mirror_pool()]))
    body = generate_latest(registry).decode()

    assert "truenas_pool_status" in body
    assert "truenas_system_info{" in body


@pytest.mark.parametrize(
    "given,expected",
    [
        ("truenas.example.com", "wss://truenas.example.com/api/current"),
        ("https://truenas.example.com", "wss://truenas.example.com/api/current"),
        ("http://truenas.example.com", "ws://truenas.example.com/api/current"),
        ("wss://nas.example.com/api/current", "wss://nas.example.com/api/current"),
        ("https://nas.example.com/api/v25.10", "wss://nas.example.com/api/v25.10"),
    ],
)
def test_url_normalisation(given, expected):
    assert build_ws_url(given) == expected


def test_url_rejects_unsupported_scheme():
    with pytest.raises(ValueError):
        build_ws_url("ftp://truenas.example.com")
