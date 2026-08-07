.PHONY: help install test lint graph graph-refresh gtfs probe inspect serve docker clean \
        data-manifest deploy-data deploy-fly

PY ?= python3
export PYTHONPATH := src

# Which city to act on. Everything below is per-city: builds, downloads and
# deployed data all live under data/<city>/.
CITY ?= dc

help:
	@echo "GetMeHome backend"
	@echo ""
	@echo "  make install       install dependencies"
	@echo "  make test          run the test suite"
	@echo "  make lint          run ruff"
	@echo "  make gtfs          download this city's GTFS feeds"
	@echo "  make graph         build the routing graph for CITY (default: dc)"
	@echo "  make graph-all     build every known city in turn"
	@echo "  make graph-refresh rebuild, re-downloading streetlights and crime"
	@echo ""
	@echo "  make probe         show what CITY's data feeds actually publish"
	@echo "  make inspect       explain CITY's lighting coverage and data age"
	@echo ""
	@echo "  Set CITY=nyc on any of the above. e.g. make graph CITY=nyc"
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

# Downloads whatever feeds CITY declares. WMATA gates its static feeds behind
# the same key as its real-time API; the MTA publishes its openly, so NYC needs
# no key for this step.
gtfs:
	$(PY) -m getmehome.ingest.fetch_gtfs --city $(CITY)

# What each feed publishes, and which column the ingester reads for every
# field it needs. Run this first when a city builds but its data looks wrong:
# a renamed column produces no error, just an empty result that looks like the
# city genuinely has no streetlights or no recent crime.
probe:
	$(PY) -m getmehome.ingest.probe --city $(CITY)

# Reads a graph that is already built. Answers the question the build summary
# compresses away: is `median lighting score: 0.0` a missing inventory, or a
# graph that covers more ground than the inventory does.
inspect:
	$(PY) -m getmehome.graph.inspect --city $(CITY)

graph:
	$(PY) -m getmehome.graph.build --city $(CITY)

graph-all:
	$(PY) -m getmehome.graph.build --all

graph-refresh:
	$(PY) -m getmehome.graph.build --city $(CITY) --refresh

serve:
	$(PY) -m uvicorn getmehome.api.main:app --host 0.0.0.0 --port 8000 --reload

docker:
	docker build -t getmehome-backend .

# The built graph is not in the image and not in git: it is large, it is
# regenerated on a different cadence than the code, and it is reproducible
# from the ingest step. So it has to be copied to the server separately.

data-manifest:
	@echo "Files the server loads at startup for $(CITY):"
	@ls -lh data/$(CITY)/build 2>/dev/null || \
		{ echo "  (nothing built — run 'make graph CITY=$(CITY)')"; exit 1; }
	@echo ""
	@echo "Total:"
	@du -sh data/$(CITY)/build
	@echo ""
	@echo "Roughly this much RAM is needed on top of the Python runtime;"
	@echo "the arrays are decompressed on load, so allow noticeably more"
	@echo "than the on-disk figure and confirm with 'docker stats'."

# Copy the build output to a host running deploy/docker-compose.yml.
#   make deploy-data HOST=root@getmehome.example.com
# --delete so a renamed or removed artefact does not linger and get loaded.
# Every built city goes across in one pass — the server discovers what it has
# from the directory layout, so a partial copy silently removes a city.
deploy-data:
	@if [ -z "$$HOST" ]; then echo "Set HOST=user@server"; exit 1; fi
	@if [ ! -d data ]; then echo "Nothing built — run 'make graph'"; exit 1; fi
	rsync -avz --delete \
		--include="*/" --include="build/**" --exclude="raw/**" --exclude="*" \
		data/ "$$HOST:~/getmehome/deploy/data/"
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
	rm -rf data/*/build
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
