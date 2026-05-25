# sif1-harness — multi-pass client-exercise pipeline.
#
# End-to-end runs in ~20 seconds on a laptop. Steps:
#   1. harness        -- notifyUpdate pass: fire 358 endpoints under luajit
#   2. aggregate      -- post-process traces -> observations.json
#   3. ui-handlers    -- initial classify + build ui_handler_map.json
#   4. ui-classes     -- invoke_classes pass against the ui-only bucket
#   5. ui-static      -- static field-extraction over UI source, with
#                        listener verification (catches success-cb closure
#                        destructures that runtime passes can't reach)
#   6. merge          -- union pass-1 + pass-2 + pass-3 -> observations.json
#   7. classify       -- final bucket assignment (4 categories)
#   8. priors         -- extract NPPS4 Pydantic schemas (needs NPPS4 src)
#   9. compare        -- diff client reads vs NPPS4 emits
#
# `make test` runs steps 1-7 and verifies invariants.
# `make compare-npps4` runs the full pipeline (1-9).

PY := uv run --no-project python
ROOT := $(shell pwd)
BUILD := build
TRACES := $(BUILD)/runtime/traces

# NPPS4_SRC = checkout of github.com/DarkEnergyProcessor/NPPS4
# DLAPI_SRC = checkout of NPPS4-DLAPI (resolves download/album/profile
#   element shapes that live outside NPPS4 main)
NPPS4_SRC ?= ./npps4
DLAPI_SRC ?= ./npps4-dlapi

.PHONY: help harness aggregate ui-classes ui-static merge classify priors compare test compare-npps4 clean

help:
	@echo "Targets:"
	@echo "  harness         Run lua harness against all 358 endpoints"
	@echo "  aggregate       Aggregate notifyUpdate traces into observations.json"
	@echo "  ui-classes      Run invoke_classes for ui-only bucket (Approach B)"
	@echo "  ui-static       Static field-extraction over UI source (Approaches D + D2)"
	@echo "  merge           Merge ui-classes + ui-static traces into observations.json"
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
# Requires classify (the bucket info ui-classes reads to pick which
# endpoints to probe). Order: harness -> aggregate -> classify --initial
# (writes coverage_classification_initial.{json,md}) -> ui-handlers
# (build map) -> ui-classes -> ui-static -> merge -> classify (writes
# canonical coverage_classification.{json,md} from final observations).
# Using a separate file for the pre-merge snapshot keeps the canonical
# coverage_classification.json from being left in a stale post-aggregate
# state when an intermediate target (ui-static, merge) is run alone.
ui-handlers: aggregate
	$(PY) src/tools/classify_coverage.py --initial
	$(PY) src/tools/find_ui_handlers.py

ui-classes: ui-handlers
	$(PY) src/tools/run_invoke_classes.py --bucket ui-only \
	  --out $(BUILD)/runtime/traces_classes

# Approach D: static field-extraction over UI source. Anchors on
# `function(arg) ... arg.response_data` inside per-call success closures,
# harvests `.<field>` reads on locals tainted from response_data,
# corpus-filters UI-wide tokens, then re-fires the listener pass with
# the harvested fields populated to mark which are listener-verified.
# The full corpus-filtered set gets unioned into observations; the
# per-field verification status (verified_by_listener vs static-only)
# rides on the merged observations record for downstream confidence
# labelling in wire-compare.
ui-static: ui-classes
	$(PY) src/tools/extract_ui_field_reads.py \
	  --out $(BUILD)/runtime/traces_static

merge: ui-static
	$(PY) src/tools/merge_observations.py
	$(PY) src/tools/classify_coverage.py

# Alias for backward-compat / muscle memory. `merge` already runs the
# final classify, so `make classify` and `make merge` are equivalent
# entry points to "run the full pipeline through the final bucket
# assignment".
classify: merge

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
	  assert n>=220, f'discovered <220 endpoints ({n}); regression?'; \
	  print(f'OK: {n} endpoints have discovered field names (floor: 220)')"
	@$(PY) -c "import json; d=json.load(open('$(BUILD)/coverage_classification.json')); \
	  by_b={}; \
	  [by_b.setdefault(c['bucket'], []).append(ep) for ep, c in d['endpoints'].items()]; \
	  hc = len(by_b.get('harness-covered', [])); \
	  assert hc >= 260, f'harness-covered regressed to {hc} (floor: 260)'; \
	  print(f'OK: {hc} endpoints harness-covered (floor: 260)')"

compare-npps4: compare

clean:
	@if [ -d "$(BUILD)/runtime" ]; then \
	  mv "$(BUILD)/runtime" "$$HOME/.Trash/sif1-runtime-$$(date +%s)"; \
	fi
	@for f in $(BUILD)/runtime_listener_observations.json \
	         $(BUILD)/runtime_listener_summary.md \
	         $(BUILD)/coverage_classification.json \
	         $(BUILD)/coverage_classification.md \
	         $(BUILD)/coverage_classification_initial.json \
	         $(BUILD)/coverage_classification_initial.md \
	         $(BUILD)/ui_handler_map.json \
	         $(BUILD)/wire_compare_static.md; do \
	  if [ -f "$$f" ]; then mv "$$f" "$$HOME/.Trash/$$(basename $$f).$$(date +%s)"; fi; \
	done
