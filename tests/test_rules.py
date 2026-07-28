"""Structural checks on the shipped alerting rules.

These are deliberately cheap: they parse `rules/truenas-alerts.yaml` and assert
invariants, with no Prometheus and no extra tooling. They run in the same
pytest job as everything else.

They cannot prove a rule *behaves* correctly — only `promtool test rules` does
that. What they can do is catch the specific regressions that already bit us,
and enforce the conventions that make a rule file usable by someone else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

RULES_FILE = Path(__file__).resolve().parents[1] / "rules" / "truenas-alerts.yaml"

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse the rules")


def load_rules() -> list[dict]:
    doc = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


def rule_named(name: str) -> dict:
    for rule in load_rules():
        if rule.get("alert") == name:
            return rule
    raise AssertionError(f"no rule named {name}")


def test_rules_file_parses_and_is_not_empty():
    rules = load_rules()
    assert len(rules) > 10
    assert all("alert" in r and "expr" in r for r in rules)


def test_every_alert_carries_a_severity_and_a_summary():
    for rule in load_rules():
        assert rule.get("labels", {}).get("severity"), (
            f"{rule['alert']} has no severity label — it cannot be routed"
        )
        assert rule.get("annotations", {}).get("summary"), (
            f"{rule['alert']} has no summary annotation — a pager with no "
            "text is not actionable"
        )


def test_severities_come_from_a_fixed_set():
    allowed = {"critical", "warning", "info"}
    for rule in load_rules():
        severity = rule["labels"]["severity"]
        assert severity in allowed, f"{rule['alert']} uses severity={severity}"


# -- the regressions that actually happened ---------------------------------


def test_scrub_age_rule_filters_to_scrub_scans():
    """Guards the bug a live appliance exposed.

    ZFS reports one scan per pool and a resilver is a scan, so aggregating
    across scan types lets a recent resilver mask a pool that has never been
    scrubbed. Without this filter the rule goes permanently silent on exactly
    the pool that most needs a scrub — one that just had a disk replaced.
    """
    expr = rule_named("TrueNASScrubTooOld")["expr"]
    assert 'function="SCRUB"' in expr, (
        "TrueNASScrubTooOld must restrict to SCRUB scans; without it a "
        "resilver silently satisfies the freshness check"
    )


def test_a_pool_with_no_scrub_record_is_still_covered():
    """The companion gap: no series means no comparison, so no alert.

    A pool that has never been scrubbed produces no SCRUB-labelled timestamp
    at all, so TrueNASScrubTooOld has nothing to evaluate. Only a set
    operation catches that.
    """
    expr = rule_named("TrueNASPoolNeverScrubbed")["expr"]
    assert "unless" in expr
    assert 'function="SCRUB"' in expr


def test_dismissed_truenas_alerts_are_excluded():
    """TrueNAS keeps dismissed alerts in the list with their occurrence counter
    still updating, so without this filter an acknowledged alert re-fires."""
    expr = rule_named("TrueNASCriticalAlert")["expr"]
    assert 'dismissed="false"' in expr


def test_redundancy_rules_survive_the_online_trap():
    """Pool status alone is not health.

    A detached mirror leg leaves the pool reporting ONLINE and healthy, so
    redundancy has to be checked through vdev shape rather than pool state.
    """
    mirror = rule_named("TrueNASMirrorDegraded")["expr"]
    assert "truenas_pool_vdev_children" in mirror
    assert 'vdev_type="MIRROR"' in mirror

    bare = rule_named("TrueNASPoolHasNoRedundancy")["expr"]
    assert 'category="data"' in bare
    assert 'vdev_type="DISK"' in bare


def test_error_counter_rules_use_increase_not_raw_values():
    """ZFS error counters reset on `zpool clear` and on import. Alerting on the
    raw value would fire forever after a single historical error and stay quiet
    after an operator clears them."""
    expr = rule_named("TrueNASDeviceErrors")["expr"]
    assert "increase(" in expr
    assert "_total" in expr


def test_every_metric_referenced_by_a_rule_is_actually_exported():
    """A rule naming a metric the exporter never emits is a rule that can never
    fire — the failure mode with no symptom."""
    import re

    collector = (
        RULES_FILE.parents[1] / "src" / "truenas_scale_exporter" / "collector.py"
    ).read_text(encoding="utf-8")
    defined = set(re.findall(r'"(truenas_[a-z_]+)"', collector))
    exported = set(defined) | {n + "_total" for n in defined}
    exported.add("truenas_system_info")

    referenced = set()
    for rule in load_rules():
        referenced.update(re.findall(r"truenas_[a-z_]+", rule["expr"]))

    unknown = sorted(referenced - exported)
    assert not unknown, f"rules reference metrics the exporter never emits: {unknown}"
