# sif1-harness — two-pass client-exercise pipeline.
#
# End-to-end runs in ~12 seconds on a laptop. Steps:
#   1. harness        -- notifyUpdate pass: fire 358 endpoints under luajit
#   2. aggregate      -- post-process traces -> observations.json
#   3. ui-handlers    -- initial classify + build ui_handler_map.json
#   4. ui-classes     -- invoke_classes pass against the ui-only bucket
#   5. merge          -- union pass-1 + pass-2 traces -> observations.json
#   6. classify       -- final bucket assignment (4 categories)
#   7. priors         -- extract NPPS4 Pydantic schemas (needs NPPS4 src)
#   8. compare        -- diff client reads vs NPPS4 emits
#
# `make test` runs steps 1-6 and verifies invariants.
# `make compare-npps4` runs the full pipeline (1-8).

PY := uv run --no-project python
ROOT := $(shell pwd)
BUILD := build
TRACES := $(BUILD)/runtime/traces

# NPPS4_SRC = checkout of github.com/DarkEnergyProcessor/NPPS4
# DLAPI_SRC = checkout of NPPS4-DLAPI (resolves download/album/profile
#   element shapes that live outside NPPS4 main)
NPPS4_SRC ?= ./npps4
DLAPI_SRC ?= ./npps4-dlapi

.PHONY: help harness aggregate ui-classes merge classify priors compare test compare-npps4 clean

help:
	@echo "Targets:"
	@echo "  harness         Run lua harness against all 358 endpoints"
	@echo "  aggregate       Aggregate notifyUpdate traces into observations.json"
	@echo "  ui-classes      Run invoke_classes for ui-only bucket (Approach B)"
	@echo "  merge           Merge ui-classes traces into observations.json"
	@echo "  classify        Bucket each endpoint (harness-covered/ui-only/etc)"
	@echo "  priors          Extract NPPS4 priors (requires NPPS4_SRC=$(NPPS4_SRC))"
	@echo "  compare         Run wire-compare static-diff vs NPPS4"
	@echo "  test            Run full pipeline (1-5), verify outputs"
	@echo "  compare-npps4   Run full pipeline (1-6)"
	@echo "  clean           Remove generated build outputs"

harness:
	$(PY) src/tools/run_lua_harness.py --all --out $(TRACES)

aggregate: harness
	$(PY) src/tools/aggregate_listener_observations.py

# Approach B: invoke ui-handler-class methods after pre-populating cache.
# Requires classify (which writes coverage_classification.json — the
# bucket info ui-classes reads to pick which endpoints to probe).
# But classify also depends on aggregate. Order is: harness -> aggregate
# -> classify (initial) -> ui-handlers (build map) -> ui-classes
# -> merge -> classify (final).
ui-handlers: aggregate
	$(PY) src/tools/classify_coverage.py
	$(PY) src/tools/find_ui_handlers.py

ui-classes: ui-handlers
	$(PY) src/tools/run_invoke_classes.py --bucket ui-only \
	  --out $(BUILD)/runtime/traces_classes

merge: ui-classes
	$(PY) src/tools/merge_observations.py

classify: merge
	$(PY) src/tools/classify_coverage.py

priors:
	NPPS4_SRC=$(NPPS4_SRC) DLAPI_SRC=$(DLAPI_SRC) \
	  $(PY) priors/extract_npps4_priors.py

compare: classify priors
	$(PY) integration/npps4/wire_compare.py \
	  --mode static-diff \
	  --out $(BUILD)/wire_compare_static.md
	@echo "----"
	@head -8 $(BUILD)/wire_compare_static.md

test: classify
	@test -f $(BUILD)/runtime_listener_observations.json \
	  || (echo "ERR: missing observations.json" && exit 1)
	@test -f $(BUILD)/coverage_classification.json \
	  || (echo "ERR: missing coverage_classification.json" && exit 1)
	@$(PY) -c "import json; d=json.load(open('$(BUILD)/coverage_classification.json')); \
	  total=d['total']; assert total==358, f'expected 358 endpoints, got {total}'; \
	  print(f'OK: {total} endpoints classified')"
	@$(PY) -c "import json; d=json.load(open('$(BUILD)/runtime_listener_observations.json')); \
	  n=sum(1 for v in d.values() if v.get('runtime_discovered_field_names')); \
	  assert n>=250, f'discovered <250 endpoints ({n}); regression?'; \
	  print(f'OK: {n} endpoints have discovered field names (v0.2.0 floor: 250)')"
	@$(PY) -c "import json; d=json.load(open('$(BUILD)/coverage_classification.json')); \
	  by_b={}; \
	  [by_b.setdefault(c['bucket'], []).append(ep) for ep, c in d['endpoints'].items()]; \
	  hc = len(by_b.get('harness-covered', [])); \
	  assert hc >= 280, f'harness-covered regressed to {hc} (v0.2.0 floor: 280)'; \
	  print(f'OK: {hc} endpoints harness-covered (v0.2.0 floor: 280)')"

compare-npps4: compare

clean:
	rm -rf $(BUILD)/runtime/
	rm -f $(BUILD)/runtime_listener_observations.json \
	      $(BUILD)/runtime_listener_summary.md \
	      $(BUILD)/coverage_classification.json \
	      $(BUILD)/coverage_classification.md \
	      $(BUILD)/ui_handler_map.json \
	      $(BUILD)/wire_compare_static.md
