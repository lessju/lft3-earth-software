# Common tasks. Python tools run in the casperfpga venv (Python 3.8); the C++/CUDA runtime builds in the lft3-runtime container.
PY      ?= /opt/lft3arth/lft3arth_venv/bin/python
PIP     ?= $(PY) -m pip
BIN     ?= $(dir $(PY))
SEARCH_BIN ?= /opt/lft3arth/search_venv/bin/lft3-search

.PHONY: help install link-tools test test-hardware lint pipeline pipeline-test viewer clean

help:
	@echo "make install        editable install of the lft3 package (+ pytest) into $(PY), then link-tools"
	@echo "make link-tools     put lft3-view, lft3-view-bench and lft3-search next to lft3-board in $(BIN)"
	@echo "make test           Python unit tests (no hardware)"
	@echo "make test-hardware  read-only tests against the live board (config/boards/rfsoc0.yaml)"
	@echo "make lint           ruff (if installed)"
	@echo "make pipeline       build the runtime (pipeline/build/lft3-capture, lft3-spectrometer, lft3-dump)"
	@echo "make pipeline-test  build + C++/CUDA unit tests on the GPU"
	@echo "make viewer         build the FBH5 browser viewer (tools/viewer/build/lft3-view), then link-tools"

install:
	$(PIP) install -e '.[dev]'
	@$(MAKE) --no-print-directory link-tools

# Commands that pip does not install: the C++ viewer, and lft3-search (installed in the separate search venv).
# Symlinks, so a rebuild or a search-venv update needs no re-link. Missing ones are skipped with a hint.
link-tools:
	@for b in lft3-view lft3-view-bench; do \
	    if [ -x tools/viewer/build/$$b ]; then ln -sfn "$(CURDIR)/tools/viewer/build/$$b" "$(BIN)$$b" && echo "linked $(BIN)$$b"; \
	    else echo "skipped $$b: not built (make viewer)"; fi; done
	@if [ -x "$(SEARCH_BIN)" ]; then ln -sfn "$(SEARCH_BIN)" "$(BIN)lft3-search" && echo "linked $(BIN)lft3-search"; \
	    else echo "skipped lft3-search: no search venv (scripts/setup_search_env.sh)"; fi

test:
	$(PY) -m pytest -q

test-hardware:
	$(PY) -m pytest -q -m hardware

lint:
	$(PY) -m ruff check src tests

pipeline:
	pipeline/scripts/build.sh

pipeline-test:
	pipeline/scripts/build.sh test

viewer:
	tools/viewer/scripts/build.sh
	@$(MAKE) --no-print-directory link-tools

clean:
	rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -not -path './pipeline/*' -prune -exec rm -rf {} +
