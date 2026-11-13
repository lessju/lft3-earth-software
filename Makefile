# Common tasks. Python tools run in the casperfpga venv (Python 3.8); the C++/CUDA plugin builds in a container.
PY      ?= /opt/lft3arth/lft3arth_venv/bin/python
PIP     ?= $(PY) -m pip

.PHONY: help install test test-hardware lint pipeline pipeline-test clean

help:
	@echo "make install        editable install of the lft3 package (+ pytest) into $(PY)"
	@echo "make test           Python unit tests (no hardware)"
	@echo "make test-hardware  read-only tests against the live board (config/boards/rfsoc0.yaml)"
	@echo "make lint           ruff (if installed)"
	@echo "make pipeline       build the CyberEther plugin (pipeline/build/lft3.cep)"
	@echo "make pipeline-test  build + C++/CUDA unit tests on the GPU"

install:
	$(PIP) install -e '.[dev]'

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

clean:
	rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -not -path './pipeline/*' -prune -exec rm -rf {} +
