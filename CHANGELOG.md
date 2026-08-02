# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Renaming or removing a metric is a **major** version bump — it silently breaks
dashboards and alerting rules downstream.

## [Unreleased]

### Fixed

- **`TrueNASPoolNeverScrubbed` no longer fires on every long scrub.** ZFS holds
  `scan.end_time` null for the whole duration of a scan, so the exporter stops
  emitting `truenas_pool_scan_end_timestamp_seconds` while a scrub is running —
  which the rule read as "no completed scrub on record". A 145 TiB pool takes
  around ten hours to scrub, so this was a guaranteed false alarm once per
  scrub, on a pool being scrubbed exactly on schedule, getting louder as the
  pool grows. The rule now exempts pools with a SCRUB scan in flight.

  The exemption is scoped to `function="SCRUB"` deliberately: during a resilver
  the single scan slot holds `RESILVER`, so the rule still fires afterwards,
  which is the case it was written for.

- **`TrueNASScrubTooOld` no longer goes blind while a scrub runs**, and its
  threshold moves from 40 days to **51**. Two independent bugs:

  - It used a bare selector, so for the same reason as above it had no series
    to evaluate for the duration of every scrub. Combined with the exemption
    added above, a scrub that starts and never finishes would have silenced
    both rules indefinitely. It now reads through `last_over_time(...[60d])`,
    which carries the last completed timestamp across the gap so the age keeps
    climbing and a hung scrub still trips the rule.
  - 40 days fires on a healthy appliance. TrueNAS's default task is weekly with
    a 35-day threshold; 35 is a multiple of 7, so the scrub normally stays
    anchored to one weekday and repeats at exactly 35 days — but anything that
    moves that anchor, such as a manual scrub, makes the task wait up to 6 more
    days for its day to come round. 41 days is the true worst case for a
    schedule that is working correctly. 51 adds ten days of headroom for a
    scrub that slows as the pool fills.

  If you have adjusted your scrub task away from the weekly/35-day default,
  re-derive the threshold as `threshold_days + 6 + headroom` rather than taking
  51 as given.

  No metric was added, renamed or removed.

## [0.2.0] - 2026-07-29

### Added

- **`boot-pool` is now collected.** `pool.query` returns data pools only, so the
  boot pool was invisible: no health metric, no vdev topology, no scrub age. It
  is now read via `boot.get_state` and emitted through the existing
  `truenas_pool_*` families rather than a `truenas_boot_pool_*` set of its own,
  so **every existing rule and dashboard panel keyed on `pool` covers it with no
  changes**. No metric was added, renamed or removed.

  Two consequences worth knowing before you upgrade:

  - On a **default single-device install, a no-redundancy rule will now fire
    against `boot-pool`** — correctly. It reads `ONLINE` and healthy while a
    checksum error on it is permanent rather than repairable, and a scrub there
    can only detect corruption, never repair it. If a single boot device is a
    trade you have made deliberately, exclude or silence the pool rather than
    dropping the metric. A mirrored boot pool is unaffected.
  - Anything that assumed pool count, or aggregated across pools without a
    selector, now sees one more pool.

### Changed

- **New privilege surface: `boot.get_state`.** It is a read, and it is the only
  call added. See [SECURITY.md](SECURITY.md), which now states the versioning
  rule for privilege changes explicitly — a new *read* call is a minor bump
  announced in the changelog; a call that *writes* would be major, and is not
  something this project intends to do.
- `truenas_up` no longer folds in the boot collector. `boot.get_state` is the
  newest and least important surface the exporter touches — 25.10 removed the
  entire SMART API in a point release — so an appliance that lacks it, or a
  TrueNAS release that drops it, must not take down visibility of the data pools
  holding everything. A boot failure now reports as
  `truenas_collector_success{collector="boot"} 0` with every other collector and
  `truenas_up` unaffected. The help text on `truenas_up` says so.

## [0.1.2] - 2026-07-28

### Fixed

- **The exporter crash-looped when the Kubernetes Service was named
  `truenas-exporter`.** The kubelet injects a legacy Docker-link variable for
  every Service in the namespace, named after the Service with dashes
  uppercased to underscores. A Service called `truenas-exporter` therefore sets
  `TRUENAS_EXPORTER_PORT=tcp://<clusterIP>:9819`, which collides exactly with
  this exporter's own port setting — and the container died at startup with
  `ValueError: invalid literal for int() with base 10: 'tcp://...'`.

  Anyone naming their release `truenas-exporter`, the most obvious name, hit
  this. Fixed at both layers:

  - The chart now sets `enableServiceLinks: false` (new `enableServiceLinks`
    value). Nothing here consumes those variables.
  - Numeric environment variables that do not parse now fall back to their
    default with a warning naming the fix, instead of raising. Raw manifests
    and Compose users can still hit the collision, so the binary must not
    depend on the chart to protect it.

## [0.1.1] - 2026-07-28

Re-release of 0.1.0. The 0.1.0 release workflow published the image but then
failed, so no chart, GitHub release, or provenance attestation was produced.
**Use 0.1.1; 0.1.0 is incomplete.**

### Fixed

- Release workflow now grants `attestations: write`, without which
  `actions/attest-build-provenance` fails with *"Resource not accessible by
  integration"* — after the image has already been pushed, leaving a partial
  release.

### Changed

- Release workflow permissions are now scoped per job rather than granted
  blanket at workflow level. The default is `contents: read`; each job requests
  only what it needs (`image` gets packages/id-token/attestations, `chart` gets
  packages, `release` gets contents). Previously every job ran with
  `contents: write` whether it needed it or not.

## [0.1.0] - 2026-07-28

**Incomplete — superseded by 0.1.1.** The image published, but the release run
failed before the chart, GitHub release, or attestation were created.

Initial release.

### Added

- Prometheus exporter for TrueNAS over the JSON-RPC 2.0 WebSocket API
  (`/api/current`, TrueNAS 25.04+), with no dependency on the REST API that is
  removed in 26.04.
- **Pool metrics** — status as a full state set, status code, health, capacity,
  fragmentation, and autotrim state.
- **Vdev metrics** — status and child count, so a mirror that has lost a leg is
  detectable while the pool still reports `ONLINE`.
- **Per-device metrics** — status, online flag, and read / write / checksum
  error counters.
- **Scrub and resilver** — state, completion timestamp, error count, progress.
- **Disk temperature**, labelled with model, serial and type.
- **TrueNAS alerts** by class and level — the only route to SMART on 25.10,
  which polls SMART inside the middleware and exposes it as alerts rather than
  as queryable attributes.
- **Scrape health** — `truenas_up`, `truenas_collector_success` per collector,
  and scrape duration. One failing collector degrades rather than blanking the
  whole scrape.
- Helm chart, published to GHCR as an OCI artifact, rendering an optional
  `ServiceMonitor` or `VMServiceScrape`. Satisfies Pod Security `restricted`
  as shipped.
- Multi-arch container image (amd64, arm64) on Chainguard base images pinned by
  digest, published with an SBOM and a build provenance attestation.
- `--once` mode, for verifying connectivity before deploying anything.
- Alerting rules in [`docs/alerts.md`](docs/alerts.md), including the
  lost-redundancy case that pool-status monitoring misses.

### Security

- Read-only by design: six middleware calls, all reads, enumerated in
  [SECURITY.md](SECURITY.md).
- API key accepted from a file, so it need not enter the environment or the
  process table.

[Unreleased]: https://github.com/scottrus/truenas-scale-exporter/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/scottrus/truenas-scale-exporter/releases/tag/v0.2.0
[0.1.2]: https://github.com/scottrus/truenas-scale-exporter/releases/tag/v0.1.2
[0.1.1]: https://github.com/scottrus/truenas-scale-exporter/releases/tag/v0.1.1
[0.1.0]: https://github.com/scottrus/truenas-scale-exporter/releases/tag/v0.1.0
