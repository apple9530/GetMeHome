.PHONY: help install test lint graph graph-refresh gtfs serve docker clean

PY ?= python3
export PYTHONPATH := src

help:
	@echo "GetMeHome backend"
	@echo ""
	@echo "  make install       install dependencies"
	@echo "  make test          run the test suite"
	@echo "  make lint          run ruff"
	@echo "  make gtfs          download WMATA GTFS feeds (needs WMATA_API_KEY)"
	@echo "  make graph         build the routing graph (downloads DC data)"
	@echo "  make graph-refresh rebuild, re-downloading streetlights and crime"
	@echo "  make serve         run the API on :8000"
	@echo "  make docker        build the container image"

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest tests/ -q

lint:
	$(PY) -m ruff check src tests

gtfs:
	@if [ -z "$$WMATA_API_KEY" ]; then \
		echo "WMATA_API_KEY is not set."; \
		echo "Get a free key at https://developer.wmata.com/ and:"; \
		echo "    export WMATA_API_KEY=..."; \
		exit 1; \
	fi
	@mkdir -p data/raw
	curl -sSf -H "api_key: $$WMATA_API_KEY" \
		"https://api.wmata.com/gtfs/bus-gtfs-static.zip" -o data/raw/wmata-bus-gtfs.zip
	curl -sSf -H "api_key: $$WMATA_API_KEY" \
		"https://api.wmata.com/gtfs/rail-gtfs-static.zip" -o data/raw/wmata-rail-gtfs.zip
	@echo "GTFS feeds downloaded to data/raw/"

graph:
	$(PY) -m getmehome.graph.build

graph-refresh:
	$(PY) -m getmehome.graph.build --refresh

serve:
	$(PY) -m uvicorn getmehome.api.main:app --host 0.0.0.0 --port 8000 --reload

docker:
	docker build -t getmehome-backend .

clean:
	rm -rf data/build
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
