.PHONY: help install test lint graph graph-refresh gtfs serve docker clean \
        data-manifest deploy-data deploy-fly

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
	@echo ""
	@echo "  make data-manifest what the server needs, and how big it is"
	@echo "  make deploy-data   rsync the built graph to a host (HOST=user@server)"
	@echo "  make deploy-fly    upload the built graph to a Fly volume"

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

# The built graph is not in the image and not in git: it is large, it is
# regenerated on a different cadence than the code, and it is reproducible
# from the ingest step. So it has to be copied to the server separately.

data-manifest:
	@echo "Files the server loads at startup:"
	@ls -lh data/build 2>/dev/null || \
		{ echo "  (nothing built yet — run 'make graph')"; exit 1; }
	@echo ""
	@echo "Total:"
	@du -sh data/build
	@echo ""
	@echo "Roughly this much RAM is needed on top of the Python runtime;"
	@echo "the arrays are decompressed on load, so allow noticeably more"
	@echo "than the on-disk figure and confirm with 'docker stats'."

# Copy the build output to a host running deploy/docker-compose.yml.
#   make deploy-data HOST=root@getmehome.example.com
# --delete so a renamed or removed artefact does not linger and get loaded.
deploy-data:
	@if [ -z "$$HOST" ]; then echo "Set HOST=user@server"; exit 1; fi
	@if [ ! -d data/build ]; then echo "Nothing built — run 'make graph'"; exit 1; fi
	rsync -avz --delete data/build/ "$$HOST:~/getmehome/deploy/data/"
	@echo ""
	@echo "Now restart the API so it loads the new graph:"
	@echo "    ssh $$HOST 'cd getmehome/deploy && docker compose restart api'"

# Same, onto a Fly volume.
deploy-fly:
	@if [ ! -d data/build ]; then echo "Nothing built — run 'make graph'"; exit 1; fi
	fly ssh console -C "mkdir -p /data/build"
	@for f in data/build/*; do \
		echo "uploading $$f"; \
		fly sftp shell <<< "put $$f /data/build/$$(basename $$f)"; \
	done
	fly apps restart

clean:
	rm -rf data/build
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
