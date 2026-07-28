# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Renaming or removing a metric is a **major** version bump — it silently breaks
dashboards and alerting rules downstream.

## [Unreleased]

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

[Unreleased]: https://github.com/scottrus/truenas-scale-exporter/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/scottrus/truenas-scale-exporter/releases/tag/v0.1.1
[0.1.0]: https://github.com/scottrus/truenas-scale-exporter/releases/tag/v0.1.0
