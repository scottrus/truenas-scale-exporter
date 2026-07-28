# Every check that runs in CI is defined here and nowhere else.
#
# The CI workflow invokes these same targets, so `make check` locally is the
# same gate a pull request faces — there is no second copy of the commands to
# drift out of sync.
#
# Tools that are not installed are skipped with a warning, so this is useful on
# a laptop without Docker. CI sets REQUIRE_ALL=1, which turns every skip into a
# failure, so a missing tool can never quietly pass in CI.

SHELL := /bin/sh

VENV        ?= .venv
PY          ?= $(VENV)/bin/python
PIP         ?= $(VENV)/bin/pip
CHART       ?= charts/truenas-scale-exporter
IMAGE       ?= truenas-scale-exporter:dev

# Read from the package rather than duplicated here, so the smoke test asserts
# the image really carries the version this working tree claims.
VERSION     := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/truenas_scale_exporter/__init__.py)

# Values every `helm template` invocation needs to satisfy the chart's own
# required-value guards.
HELM_MIN := --set truenas.url=truenas.example.com --set truenas.apiKey=dummy

.DEFAULT_GOAL := help

# $(call missing,<binary>,<check name>) — expands to a shell fragment that
# fails under REQUIRE_ALL and otherwise reports a skip. It must be used inside
# the *same* shell invocation as the work it guards; make runs each recipe line
# in its own shell, so an early `exit` on one line would not skip the next.
define missing
if [ -n "$(REQUIRE_ALL)" ]; then \
  echo "ERROR: $(1) is required but not installed" >&2; exit 1; \
else echo "SKIP: $(2) ($(1) not installed)"; fi
endef

.PHONY: help
help:
	@echo "Local validation — mirrors the PR checks exactly."
	@echo
	@echo "  make setup         create $(VENV) and install the package + dev deps"
	@echo "  make check         run everything below; the full PR gate"
	@echo
	@echo "  make demo          collect once against a fake TrueNAS — no appliance needed"
	@echo "  make demo-serve    same, but serve /metrics on :9819 until Ctrl-C"
	@echo
	@echo "  make lint          ruff check, ruff format --check, pylint"
	@echo "  make fmt           apply ruff formatting and autofixes"
	@echo "  make test          pytest"
	@echo "  make actionlint    validate workflow syntax, expressions, run: blocks"
	@echo "  make actions-pinned  every uses: is SHA-pinned with a version comment"
	@echo "  make rules         promtool rule tests (opt-in; NOT in check or CI)"
	@echo "  make helm          helm lint, template permutations, kubeconform"
	@echo "  make docker        hadolint, image build, smoke test"
	@echo "  make scan          grype CVE scan (run 'make docker' first)"
	@echo
	@echo "  REQUIRE_ALL=1      turn 'tool not installed' skips into failures"

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

.PHONY: setup
setup: $(VENV)
	@$(PIP) install --quiet -e ".[dev]"

# ---------------------------------------------------------------- python ----

.PHONY: lint
lint: setup
	@echo "==> ruff check"
	@$(PY) -m ruff check --output-format=concise .
	@echo "==> ruff format --check"
	@$(PY) -m ruff format --check .
	@echo "==> pylint"
	@$(PY) -m pylint src/truenas_scale_exporter

.PHONY: fmt
fmt: setup
	@$(PY) -m ruff format .
	@$(PY) -m ruff check --fix .

# --------------------------------------------------------------- workflows ----
# actionlint validates workflow syntax, `${{ }}` expressions, and action input
# names, and runs shellcheck over every `run:` block. Workflows are the one
# artifact here with no other test — they only execute on GitHub.

.PHONY: actionlint
actionlint:
	@if ! command -v actionlint >/dev/null 2>&1; then $(call missing,actionlint,actionlint); \
		echo "     install with: brew install actionlint"; \
	else \
		echo "==> actionlint"; \
		actionlint -color; \
		echo "    workflows valid"; \
	fi

# Every `uses:` must be pinned to a 40-character commit SHA. A floating tag is
# mutable: the same workflow can run different code tomorrow, which is the
# supply-chain hole SHA pinning exists to close.
.PHONY: actions-pinned
actions-pinned:
	@echo "==> action pinning"
	@unpinned=$$(grep -hoE "uses: +[^ ]+" .github/workflows/*.yml \
		| awk '{print $$2}' \
		| grep -vE "@[0-9a-f]{40}$$" || true); \
	if [ -n "$$unpinned" ]; then \
		echo "FAIL: not pinned to a commit SHA:"; echo "$$unpinned" | sed 's/^/      /'; \
		exit 1; \
	fi; \
	missing_comment=$$(grep -hE "uses: +[^ ]+@[0-9a-f]{40}" .github/workflows/*.yml \
		| grep -vE "# *v[0-9]" || true); \
	if [ -n "$$missing_comment" ]; then \
		echo "FAIL: pinned but missing a '# vX.Y.Z' comment:"; \
		echo "$$missing_comment" | sed 's/^ */      /'; \
		exit 1; \
	fi; \
	echo "    all $$(grep -chE 'uses:' .github/workflows/*.yml | paste -sd+ - | bc) uses: are SHA-pinned with a version comment"

.PHONY: test
test: setup
	@echo "==> pytest"
	@$(PY) -m pytest -q

# ------------------------------------------------------------------ demo ----
# Try the exporter with no appliance and no credentials. The fake serves the
# same six read-only methods with response shapes copied from a real 25.10.5,
# including a pool that reports healthy while having lost all redundancy.

.PHONY: demo
demo: setup
	@echo "==> starting fake TrueNAS on ws://127.0.0.1:8765"
	@$(PY) scripts/fake_truenas.py >/tmp/fake-truenas.log 2>&1 & \
	  echo $$! > /tmp/fake-truenas.pid; \
	  sleep 2; \
	  echo "==> collecting once"; \
	  $(VENV)/bin/truenas-scale-exporter \
	      --url ws://127.0.0.1:8765 --api-key demo --once; \
	  status=$$?; \
	  kill "$$(cat /tmp/fake-truenas.pid)" 2>/dev/null || true; \
	  rm -f /tmp/fake-truenas.pid; \
	  exit $$status

.PHONY: demo-serve
demo-serve: setup
	@echo "==> fake TrueNAS + exporter on http://127.0.0.1:9819/metrics"
	@echo "    Ctrl-C to stop both."
	@$(PY) scripts/fake_truenas.py >/tmp/fake-truenas.log 2>&1 & \
	  echo $$! > /tmp/fake-truenas.pid; \
	  trap 'kill "$$(cat /tmp/fake-truenas.pid)" 2>/dev/null; rm -f /tmp/fake-truenas.pid' EXIT INT TERM; \
	  sleep 2; \
	  $(VENV)/bin/truenas-scale-exporter --url ws://127.0.0.1:8765 --api-key demo

# ----------------------------------------------------------------- rules ----
# Alerting rules are the one artifact whose bugs the Python tests cannot reach:
# a wrong PromQL expression is not a wrong dict-to-metric mapping. `promtool
# test rules` evaluates them against synthetic series, which is the only way a
# rule that silently never fires gets caught before production does it.

.PHONY: rules
rules: rules-promtool rules-pint

.PHONY: rules-promtool
rules-promtool:
	@if ! command -v promtool >/dev/null 2>&1; then $(call missing,promtool,promtool); \
		echo "     install with: brew install prometheus"; \
	else \
		set -e; \
		echo "==> promtool check rules"; \
		promtool check rules rules/*.yaml; \
		echo "==> promtool test rules"; \
		promtool test rules tests/rules/*_test.yaml; \
	fi

# pint is complementary, not duplicative: promtool proves a rule behaves
# correctly on series you hand it, while pint statically analyses the rule
# itself. Pointed at a live Prometheus (--config with a prometheus block) it
# also flags selectors that match nothing real — which is the failure mode
# that produced the resilver-masking bug in the first place.
.PHONY: rules-pint
rules-pint:
	@if ! command -v pint >/dev/null 2>&1; then $(call missing,pint,pint); \
		echo "     install with: brew install pint"; \
	else \
		echo "==> pint lint"; \
		pint --no-color lint rules/*.yaml; \
		echo "    rules linted"; \
	fi

# ------------------------------------------------------------------ helm ----

.PHONY: helm
helm: helm-lint helm-template helm-required helm-schema

.PHONY: helm-lint
helm-lint:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,helm lint); else \
		echo "==> helm lint"; \
		helm lint $(CHART) $(HELM_MIN); \
	fi

.PHONY: helm-template
helm-template:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,helm template); else \
		echo "==> helm template permutations"; \
		set -e; \
		helm template t $(CHART) $(HELM_MIN) > /tmp/tnse-inline.yaml; \
		grep -q 'kind: Secret' /tmp/tnse-inline.yaml \
			|| { echo "FAIL: inline apiKey did not render a Secret"; exit 1; }; \
		helm template t $(CHART) --set truenas.url=truenas.example.com \
			--set truenas.existingSecret=external > /tmp/tnse-external.yaml; \
		if grep -q 'kind: Secret' /tmp/tnse-external.yaml; then \
			echo "FAIL: existingSecret must not render a Secret"; exit 1; fi; \
		helm template t $(CHART) $(HELM_MIN) \
			--set serviceMonitor.enabled=true \
			--set vmServiceScrape.enabled=true > /tmp/tnse-scrapes.yaml; \
		grep -q 'kind: ServiceMonitor' /tmp/tnse-scrapes.yaml \
			|| { echo "FAIL: ServiceMonitor not rendered"; exit 1; }; \
		grep -q 'kind: VMServiceScrape' /tmp/tnse-scrapes.yaml \
			|| { echo "FAIL: VMServiceScrape not rendered"; exit 1; }; \
		echo "    all permutations rendered as expected"; \
	fi

.PHONY: helm-required
helm-required:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,required-value guards); else \
		echo "==> required values are enforced"; \
		if helm template t $(CHART) --set truenas.apiKey=dummy >/dev/null 2>&1; then \
			echo "FAIL: chart rendered without truenas.url"; exit 1; fi; \
		if helm template t $(CHART) --set truenas.url=x >/dev/null 2>&1; then \
			echo "FAIL: chart rendered without an API key"; exit 1; fi; \
		echo "    both guards fired"; \
	fi

.PHONY: helm-schema
helm-schema:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,kubeconform); \
	elif ! command -v kubeconform >/dev/null 2>&1; then \
		$(call missing,kubeconform,kubeconform); \
		echo "     install with: brew install kubeconform"; \
	else \
		echo "==> kubeconform"; \
		helm template t $(CHART) $(HELM_MIN) \
			| kubeconform -strict -summary -schema-location default \
				-skip ServiceMonitor,VMServiceScrape; \
	fi

# ---------------------------------------------------------------- docker ----

.PHONY: docker
docker: docker-lint docker-build

.PHONY: docker-lint
docker-lint:
	@if ! command -v hadolint >/dev/null 2>&1; then $(call missing,hadolint,hadolint); \
	else \
		echo "==> hadolint"; \
		hadolint --failure-threshold warning Dockerfile; \
	fi

.PHONY: docker-build
docker-build:
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,docker build); else \
		set -e; \
		echo "==> docker build"; \
		docker build -t $(IMAGE) .; \
		echo "==> image smoke test"; \
		docker image inspect $(IMAGE) >/dev/null; \
		docker run --rm $(IMAGE) --version | grep -q "$(VERSION)"; \
		echo "    reports version $(VERSION)"; \
		docker run --rm --entrypoint python $(IMAGE) -c \
			'import truenas_scale_exporter as m; print(m.__version__)' >/dev/null; \
		echo "    package imports inside the image"; \
		out="$$(docker run --rm $(IMAGE) 2>&1 || true)"; \
		case "$$out" in \
			*"No TrueNAS URL supplied"*) \
				echo "    refuses to start unconfigured, with the expected message";; \
			*) echo "FAIL: unconfigured run said: $$out"; exit 1;; \
		esac; \
		if docker run --rm $(IMAGE) >/dev/null 2>&1; then \
			echo "FAIL: expected a non-zero exit with no configuration"; exit 1; fi; \
		echo "    runs as uid $$(docker run --rm --entrypoint python $(IMAGE) -c 'import os;print(os.getuid())')"; \
	fi

.PHONY: scan
scan:
	@if ! command -v grype >/dev/null 2>&1; then $(call missing,grype,grype); \
		echo "     install with: brew install grype"; \
	else \
		set -e; \
		echo "==> grype"; \
		docker image inspect $(IMAGE) >/dev/null 2>&1 \
			|| { echo "FAIL: $(IMAGE) not built — run 'make docker' first"; exit 1; }; \
		grype $(IMAGE) --only-fixed --fail-on high; \
	fi

# ----------------------------------------------------------------- gates ----

.PHONY: check
check: lint actionlint actions-pinned test helm docker
	@echo
	@echo "All available checks passed."
	@if [ -z "$(REQUIRE_ALL)" ]; then \
		echo "Note: anything reported as SKIP above was not run."; fi

.PHONY: clean
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
