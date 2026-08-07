# GetMeHome

Safety-aware pedestrian and public transport routing for Washington, DC.

Most navigation apps answer "what is the fastest way there?". This one also
answers "what is the safest way there, and what does that cost me in time?" —
then navigates you along whichever you pick.

Routes are scored on how well lit they are, how much crime the area has seen,
and how exposed the streets themselves are. You get several options with
comparable scores, a plain-language explanation of the tradeoff, and
turn-by-turn guidance with voice.

There is also an optional privacy layer: automated licence-plate-reader (ALPR)
cameras can be shown on the map and routed around.

---

## Status, and what has actually been verified

Worth being straight about this before you invest time in it.

| Part | State |
|---|---|
| Backend routing, safety model, transit, crime grid, search, sharing, API | Written and covered by 204 passing tests |
| `RouteTracker` navigation maths | Algorithm validated independently against hand-computed cases |
| iOS app | Compiles and launches; UI beyond that not exercised here |
| The DC data build (`make graph`) | Streetlight + crime ingestion fixed against the real feeds |
| WMATA **real-time** (live buses, predictions) | Written against the published API, **never run against it** — see below |
| Every **New York** data source | Written against published schemas, **never run against them** — see below |

Nothing New York touches has been run against a real feed. NYC Open Data, the
MTA's GTFS endpoints and `api.wmata.com` were all unreachable from the build
environment, so every NYC schema — NYPD's complaint columns, DOT's street light
columns, the MTA's feed URLs — comes from published documentation rather than
from a response anyone has seen. The DC path is unaffected and still covered.

Expect the first `make graph CITY=nyc` to need a column name changed. The build
is written for that: every field goes through a candidate list, and it logs
loudly when a large share of incidents fall outside the crime vocabulary, which
is what a renamed offence column looks like.

The real-time feeds have the same gap in both cities: Everything there fails soft by design — a wrong field name
degrades to scheduled times rather than to an error — so the failure mode is
"live never appears", not a broken app. If that is what you see, the parsing in
`ingest/wmata_live.py` is the first place to look.

The environment this was built in had no macOS or Swift toolchain, and its
network policy blocked `maps2.dcgis.dc.gov`, `opendata.dc.gov`,
`api.wmata.com` and Geofabrik. So the ingestion code is written against DC's
published schemas and is deliberately schema-tolerant, but the first real
`make graph` is likely to need a small fix or two — most plausibly a column
name in the DDOT streetlight layer. `ArcGisClient.find_layers` and
`pick_field` exist precisely so that turns into a one-line change rather than
a rewrite.

The iOS app has since been confirmed to compile and launch on the simulator.

---

## Architecture

```
        iOS app (SwiftUI, iOS 17+)
        map · route options · turn-by-turn · voice
                      │  HTTP/JSON
                      ▼
        FastAPI backend
        risk-weighted A*  ·  RAPTOR transit  ·  scoring
                      │
                      ▼
        Scored graph  (built offline, ~10^5 segments)
                      ▲
        ┌─────────────┼──────────────┬─────────────┐
   OpenStreetMap  DDOT lights   MPD crime      WMATA GTFS
   (+ ALPR nodes)
```

The routing lives on the server rather than the phone for one reason worth
naming: **Apple's MapKit cannot do this**. `MKDirections` returns Apple's
routes and gives you no way to re-weight the underlying edges, so a genuine
safety router has to own its own graph. Once you own the graph, putting it
behind an API means crime data refreshes daily without an App Store release.

---

## Cities

Two: **Washington, DC** and **New York City**. The app asks which on first
launch and it can be changed from Settings; the server serves either, and can
serve both at once.

Adding a city means adding a record to `backend/src/getmehome/cities.py` — its
extent, projection origin, data sources and crime vocabulary — plus an ingest
adapter if its feeds are on a platform not already supported. Nothing else in
the codebase learns its name.

### What actually differs between cities

Three things, and each of them fails silently rather than loudly if it is got
wrong, which is why they are called out here.

**The projection.** Distances are computed in a local equirectangular frame,
and its east-west scale is `cos(origin_lat)`. This used to be a module-level
constant at DC's centre. Projecting New York through it shrinks every east-west
distance by 2.6% — about a kilometre across the city, in a direction nothing
would ever flag as an error. So each city has its own frame, and it is **stored
with the data** rather than looked up: a graph carries the frame its node
coordinates were built in, as does the crime raster and the hex grid. Loading a
graph cannot pick up the wrong one.

The polyline helpers — length, resampling, simplification — take no frame at
all. Measuring one line only needs a frame local to that line, so they anchor
on its own first vertex. That is correct in any city with nothing to thread
through, and marginally more accurate than a city-wide frame even at home.

**The crime vocabulary.** MPD publishes `ASSAULT W/DANGEROUS WEAPON` and
`THEFT F/AUTO`; NYPD publishes `FELONY ASSAULT` and `GRAND LARCENY OF MOTOR
VEHICLE`. Different vocabularies for different legal categories, with less than
a third overlap. One shared table would fall through to the default weight for
every offence in the other city and turn the crime surface into a map of where
people are. Each city carries its own severity, pedestrian-relevance, display-
name and category tables; the build refuses to run if they are internally
inconsistent, and warns when real incidents land outside the vocabulary —
which is the exact symptom of a table written against the wrong city.

Two NYPD-specific judgements worth naming: `FELONY ASSAULT` is the violent one
and sits with robbery, while plain `ASSAULT 3 & RELATED OFFENSES` is a
misdemeanour covering a lot of non-street conduct and is weighted far lower;
and grand versus petit larceny is a distinction about value taken rather than
about risk to a passer-by, so both sit near the bottom.

**Time of day.** MPD stamps every incident with a shift. NYPD stamps a
timestamp, so a shift is derived from the hour — using MPD's own tour
boundaries, which is what keeps the two cities' night surfaces comparable
rather than one being a police tour and the other an arbitrary cut.

**Where it happened.** NYPD publishes `prem_typ_desc`; MPD publishes nothing
equivalent. This turned out to matter more than expected. A large share of New
York's reported sexual offences and assaults occur inside dwellings, and a
domestic assault on the eleventh floor says close to nothing about the risk of
walking past the building. Counting it does two wrong things at once: it
inflates residential streets, and it maps the location of *housing* rather than
of street crime.

So New York's incidents are weighted by premises, in three bands:

| | Weight | |
|---|---|---|
| Outdoor | 1.0 | street, park, parking lot, highway |
| Semi-public | 0.55 | bar, shop, restaurant, subway interior |
| Private dwelling | **0.0** | residence, apartment, public housing |
| Unknown | 1.0 | no premises recorded |

Zero for dwellings is an exclusion rather than a very small number, because the
question the model answers is "what is the risk of walking down this street"
and a crime inside someone's home is not evidence about that. Semi-public sits
between: a robbery outside a bar at 1am is squarely pedestrian-relevant even
though the report names the bar.

Unknown counts in full, which is what makes Washington's feed unaffected — it
has no premises column, so its policy is the identity and nothing changes.
Because "unknown" is generous, a *wrong column name* would silently disable the
whole filter, so the build fails loudly if a city that declares a premises
policy sees more than half its incidents come back without one.

The related fix: NYPD's `SEX CRIMES` is a much broader bucket than its name
suggests — forcible touching sits in it alongside far graver offences, and it is
several times the size of the rape category. Weighting it near homicide, as the
first pass did, let a handful of reports dominate every cell they appeared in
and produced sexual-offence shares that did not survive a sense check against
DC. It is now weighted below rape while staying serious and staying in the
sexual category. Both codes carry the same display name and the map's breakdown
groups rows by label, so they appear as one line rather than as two adjacent
rows reading "Sexual offense".

### Data sources

| | Washington | New York |
|---|---|---|
| Streets | Geofabrik DC extract | Geofabrik New York State extract |
| Crime | MPD, ArcGIS MapServer | NYPD complaints, Socrata (`5uac-w243`, `qgea-i56i`) |
| Streetlights | DDOT, ArcGIS | DOT street light poles, Socrata |
| Transit (static) | WMATA rail + bus GTFS | MTA subway + five borough bus GTFS |
| Transit (real-time) | WMATA JSON REST | MTA GTFS-Realtime protobuf |

The ingest adapter is chosen on the *platform* — ArcGIS or Socrata — rather
than on the city, so a third city on either is no new ingest code.

### Real-time is two protocols, not two URLs

WMATA serves JSON over REST; the MTA serves GTFS-Realtime protobuf. The
`LiveProvider` interface is written around what both can answer, and the MTA
implementation is better in one respect: GTFS-RT carries the static feed's own
`trip_id`, so predictions join the timetable **exactly**. WMATA's real-time trip
ids do not reliably match its static feed, so predictions there are matched on
route and rough time — a heuristic that occasionally attaches a prediction to
the wrong departure.

Neither agency publishes train coordinates, so a train's position is estimated
between stations in both cities and labelled as an estimate. Buses report real
positions in both.

The MTA client needs `gtfs-realtime-bindings`, which is an optional dependency:
a Washington-only deployment has no reason to install protobuf, and its absence
disables live NYC data rather than breaking the import.

### Building and serving more than one

```bash
make graph CITY=dc          # or CITY=nyc, or `make graph-all`
make gtfs  CITY=nyc         # six feeds for New York, two for DC
```

Artefacts land in `data/<slug>/build`, so cities cannot overwrite each other.
The server discovers what is built from that layout.

Cities **load lazily and are never evicted**. Holding both resident costs the
memory of the larger, and New York's is considerably larger — its OSM extract
covers the whole state. A deployment serving one city should not pay for the
other. The cost is a slow first request per city after a restart, which is why
the app asks for a city up front rather than on the first route, and why
`GETMEHOME_PRELOAD_CITIES` exists for deployments that would rather pay it at
boot.

---

## How the safety score works

The headline number is 0–100, higher is safer. It is a length-weighted blend
of three factors, and the app shows the breakdown rather than just the number.

### Lighting

Each DDOT streetlight is modelled as a point source with inverse-square
falloff and its mounting height folded in:

```
illuminance = Σ  lumens / (distance² + height²)
score       = 1 − exp(−illuminance / reference)
```

Lamp spacing is what this is really measuring. A street with four lamps
clustered at one end and eighty metres of darkness is not the same as one with
four evenly spaced lamps, and a "count lamps within 50 m" heuristic cannot tell
them apart. Each segment is sampled every 12 m and its score blends the mean
with the 20th percentile, so a single bright lamp cannot mask a dark stretch.

That absolute score is then **ranked against the rest of the city**, and this
is the part that makes night scoring work at all. DC lights nearly all of its
streets, so on an absolute scale almost every segment lands near the top of the
curve and every night route comes back looking fine. The differences between
routes are real, but they live in the last few percent of the range where the
other factors swamp them. A percentile answers the question a pedestrian is
actually asking — *is this darker than the alternative* — and spreads the
segments evenly across [0, 1] so the difference survives into the score.

Darkness then enters the risk as `(1 − lit) ^ 0.6`. The exponent is below one,
so the curve rises steeply out of zero: a street somewhat worse lit than its
neighbours already carries a large share of the penalty rather than a
proportional sliver. Together with a night lighting weight of 1.30, a route
with visibly fewer lamps is penalised hard rather than slightly.

Lighting carries **zero weight in daylight**, switched on real solar elevation
rather than a clock hour — DC sunset moves by nearly three hours across the
year.

### Crime

MPD incidents as a rasterised Gaussian KDE, inside a lookback window the user
picks: **30 days, 2 months, 6 months, or a year**. Only incidents inside the
window count, and each window is scored and normalised against its own
distribution — so "the worst areas in the last 30 days" means exactly that,
rather than a faded copy of the annual picture. The window applies to the map
grid and to route scoring together; a map showing a year of incidents beside a
route scored on thirty days would be actively misleading.

Windows are baked into the graph at build time, one pair of surfaces (day and
night) per window. This is not an optimisation — a KDE over the whole city
takes seconds, and the router needs every segment scored before it takes its
first step.

The shorter windows are noisier, and that tradeoff belongs to the user. A month
of DC data is a few thousand incidents over 177 km²; a year is stable but slow
to notice a neighbourhood changing.

Inside a window each incident is weighted by:

- **severity** — homicide and sexual abuse 1.0, assault with a dangerous
  weapon 0.90, robbery 0.85, then a deliberately wide gap down to burglary
  0.15 and vehicle theft 0.08. A stolen car and a sexual assault are not two
  points on one scale of "how bad": to someone choosing which street to walk
  down at night they are barely the same kind of information. Property crime
  stays non-zero because a street with a lot of it usually has little passive
  supervision, which is a weak but real signal.
- **pedestrian relevance**, kept deliberately separate — a burglary is a
  serious crime that says relatively little about the risk to someone walking
  past, so it carries 0.25 here while a street robbery carries 1.0. Multiplied
  through, the least serious violent offence still outweighs the worst
  property offence by more than 20x.
- **weapon** — a gun multiplies by 1.4, a knife by 1.2

There is deliberately **no recency decay inside a window**. The window is the
recency filter; decaying on top of it would quietly weight day 1 against day 29
of a period the user asked to treat as one.

Day and night surfaces are built separately from MPD's shift field, because
the streets that are risky at 2am are not the ones that are risky at 2pm.
Density is normalised against the 97th percentile rather than the maximum, so
one extreme hotspot cannot flatten the rest of the city to zero.

Every weight above lives in `backend/src/getmehome/config.py`. They are
judgement calls, and you should feel free to disagree with them.

### Street exposure

Derived from OSM tags: tunnels, alleys, unpaved paths through green space,
missing pavements. This is the only signal available where there is neither a
lamp within 45 m nor an incident within 450 m, which is a surprising amount of
the city.

### Routing on it

```
cost(edge) = length / speed × (1 + λ × risk)
```

`λ` is the risk-aversion dial. The three options come from three genuine
searches at λ = 0, 2 and 7 — not one route bent slightly — so each is
optimal for some coherent preference. Near-identical options collapse into one
card, and detours are capped at 2.2× the fastest route so "safest" cannot
become an absurd diversion.

### Transit

RAPTOR over the WMATA GTFS feed, carrying two clocks. Real wall-clock time
governs every feasibility test, so returned itineraries are always physically
catchable. A second risk-penalised clock governs comparison and pruning, so
the search prefers journeys whose walking and waiting happen in safer places.

Access, egress and transfer walks are costed by the same safety model as any
other walk, and **time spent waiting at a stop is priced by that stop's risk**
— standing at an unlit bus stop for twelve minutes is real exposure that a
walking-only model misses entirely.

---

## Licence plate readers

Camera positions come from the OpenStreetMap ALPR tagging convention that the
[DeFlock](https://deflock.me) project popularised (`man_made=surveillance` +
`surveillance:type=ALPR`), including the mapped `direction`.

Exposure is modelled as a **directional cone**, not a blob — a camera pointed
north down a one-way street cannot see the street a block south, and treating
it as omnidirectional would both overstate coverage and cause pointless
detours. Cameras with no mapped bearing are treated as omnidirectional at a
discount and drawn as a ring rather than a wedge, so the map never implies a
direction the data does not have.

This is kept **entirely separate from the safety score**. Passing a camera is
a privacy consideration, not a danger, and merging the two would make both
numbers mean less. It is its own overlay and its own opt-in cost term.

**The data is crowdsourced and incomplete.** A street with no camera shown may
well have one, and not every plate reader is operated by Flock. The app says
so at the point of use, and so should you if you build on this.

---

## The crime grid

Incidents are binned into hexagons that can be tapped for what was actually
reported there — the count of violent and sexual offences, the share that
happened at night, the most recent date, and a breakdown by offence type.

That breakdown is **ordered by weighted contribution, not by raw count**, and
each row shows its share of the cell's risk. A block with forty car break-ins
and one robbery lists the robbery first, because that is where the risk
actually is; ordering by count would bury it.

Hexagons rather than squares because every neighbour of a hexagon is the same
distance away and shares a full edge, so a cluster reads the same whichever way
it is oriented. A square grid has neighbours at two different distances (edge
versus corner), which makes diagonal clusters look weaker than identical
horizontal ones.

Two details that matter for how it feels:

* **The grid is anchored to the projection origin**, not to the viewport, so
  cells stay put while you pan rather than reflowing under your finger.
* **Cell size comes from a fixed ladder** and adapts to zoom, with a 165 m
  floor and a 180-cell ceiling. Both limits exist because each cell is a
  separate filled map overlay and render cost, not payload size, is what
  binds — the same mistake the per-street overlay this replaced made at a
  larger scale. Only the selected cell is stroked, for the same reason: an
  outline is a second draw pass per polygon.

Cell colour is a sequential single-hue ramp, not a rainbow: intensity is an
ordered quantity, and a rainbow implies category boundaries that do not exist.

---

## Setup

### Backend

```bash
cd backend
make install
```

Get a free WMATA API key at <https://developer.wmata.com/>, then:

```bash
export WMATA_API_KEY=your_key_here
make gtfs      # downloads the bus and rail GTFS feeds
make graph     # downloads OSM + DDOT + MPD, then builds the scored graph
make serve     # http://localhost:8000
```

`make graph` takes 10–25 minutes the first time, mostly paging through three
years of crime records. Raw downloads are cached in `backend/data/raw/`, so
re-running after a code change is fast. Use `make graph-refresh` to re-pull the
source data.

Sanity check:

```bash
curl localhost:8000/health
curl localhost:8000/meta
```

Run the tests any time — they need no data build:

```bash
make test
```

### iOS

```bash
brew install xcodegen

# Xcode needs a signing team. Find yours in Xcode > Settings > Accounts, or
# at developer.apple.com/account under Membership.
export DEVELOPMENT_TEAM=ABCDE12345

cd ios && xcodegen generate && open GetMeHome.xcodeproj
```

Setting `DEVELOPMENT_TEAM` in your shell rather than picking the team in
Xcode's Signing tab means the choice survives regeneration — `xcodegen
generate` overwrites the `.xcodeproj`, taking any manual signing setup with
it. Put the export in `~/.zshrc` so it persists.

Selecting the team in Xcode works too; you will just have to redo it after
every regeneration.

On the **simulator**, the default server URL (`http://localhost:8000`) works as
is. On a **physical device**, localhost is the phone itself — open Settings
inside the app and point it at your Mac's LAN address:

```bash
ipconfig getifaddr en0     # e.g. 192.168.1.42
```

then set the server to `http://192.168.1.42:8000`. Both devices must be on the
same Wi-Fi, and iOS will prompt for Local Network permission the first time.

Settings has a **Test connection** button that reports what the server
actually replied with — including whether it has a graph, streetlights, a
search index and transit loaded. Use it before assuming the app is at fault.

Without a paid Apple Developer account you can still run on a device, but the
provisioning profile expires every 7 days and you will need to re-install.

---

## Running it somewhere permanent

`make serve` is a development server on your laptop. To have installed apps
connect to a backend that is simply always there, two things have to happen:
the server needs a stable HTTPS address, and the app needs to be built knowing
that address.

### The app finds the server by itself

The server URL is baked into the build from an environment variable:

```bash
export GETMEHOME_SERVER_URL=https://getmehome.example.com
cd ios && xcodegen generate
```

That writes `GetMeHomeServerURL` into `Info.plist`, and every install connects
on first launch with nobody typing anything. The field in Settings stays as a
per-install override, which is what you want for development and for anyone
running their own backend. Leave the variable unset and it falls back to
`http://localhost:8000` — right for the simulator, wrong on a device, where
localhost *is* the phone.

**HTTPS is not optional here.** The client currently ships
`NSAllowsArbitraryLoads` so it can reach a laptop over plain HTTP while you are
developing. That has to come out of `project.yml` before you release, and once
it does an `http://` backend stops working at all.

### A VPS with Docker

`deploy/` has a two-container compose file: the API, and Caddy in front of it
obtaining and renewing a Let's Encrypt certificate on its own.

```bash
# On the server
git clone <this repo> getmehome && cd getmehome/deploy
cp .env.example .env && $EDITOR .env          # domain, email, WMATA key
docker compose up -d

# From your machine, once `make graph` has run locally
cd backend && make deploy-data HOST=root@your-server
ssh root@your-server 'cd getmehome/deploy && docker compose restart api'
```

The API is never published to the host — only Caddy can reach it — so there is
no way to hit it over plain HTTP even by accident.

### AWS, on your own domain

**[`deploy/aws.md`](deploy/aws.md)** is a start-to-finish walkthrough: rent a
Lightsail instance, point a subdomain at it, install Docker, ship the built
data up, and get HTTPS. Written assuming no prior AWS knowledge.

The one thing worth knowing before you start: **build the data on your machine
and copy it up, never on the server.** Parsing New York's OSM extract needs
several gigabytes of memory, which a small instance does not have, and the
build gets killed halfway with a message that does not explain why.

### Fly.io

`deploy/fly.toml` is the managed equivalent: HTTPS, a persistent volume and an
anycast address without running a host.

```bash
fly launch --no-deploy --copy-config
fly volumes create getmehome_data --size 5 --region iad
fly secrets set WMATA_API_KEY=... GETMEHOME_PUBLIC_URL=https://<app>.fly.dev
fly deploy
cd backend && make deploy-fly
```

`iad` is Ashburn, next door to DC — which is the entire map, so there is no
reason to serve it from anywhere else.

### Things that will bite you

**The graph is not in the image.** It is large, reproducible from the ingest
step, and rebuilt on a different cadence than the code, so it is copied to the
server separately — that is what `make deploy-data` is for. `make data-manifest`
lists what the server needs and how big it is. Restart the API after copying;
everything is loaded once at startup and held in memory.

**Memory is the sizing constraint, not CPU.** The graph, the crime points and
the timetable are all resident. The arrays are decompressed on load, so allow
noticeably more than the on-disk size, and confirm against your own build with
`docker stats` or `fly status`. Too small a machine is OOM-killed during startup
rather than failing in a way that names the cause.

**One worker, deliberately.** ETA shares live in process memory, so a second
worker would answer "no such share" for half the requests on a live share, and
would double the memory for throughput a single async worker already provides
at this traffic. Scaling past one instance means moving shares to Redis first.

**Set `GETMEHOME_PUBLIC_URL`.** Share links are built from it. Behind a proxy
the request's own scheme and host are the proxy's internal ones, so without it
a link comes out as `http://api:8000/s/...` — unreachable, and downgraded from
HTTPS for a URL carrying a live location.

**Swap the geocoder before real traffic.** The default is the public Nominatim
instance, which rate-limits hard and asks you not to lean on it. Point
`GETMEHOME_GEOCODER_URL` at your own or a paid one. Most queries are answered
by the local index anyway; this only affects the top-up.

**Rebuild the data periodically.** Crime moves, and the GTFS timetable is
loaded for a single service date at build time, so departure boards drift as
schedules change. A weekly `make graph-refresh && make deploy-data` is a
reasonable cadence; nothing does it for you.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /route` | Plan itineraries. Body takes origin, destination, modes, `avoidCameras`, optional `departAt`, `forceNight` and `crimeWindowDays`. |
| `GET /cameras` | Flock/ALPR cameras in a bbox, for the overlay. |
| `GET /crime/grid` | Incidents binned into hexagons over a bbox. Takes `windowDays` and `nightOnly`. |
| `GET /crime/pack` | The whole city's grid at every window, for offline use. Cached and ETagged. |
| `GET /transit/stops` | Metro and bus stops in a bbox, capped and reporting whether it capped. |
| `GET /transit/stop/{id}/board` | The next departures from a stop, with live predictions folded in. |
| `GET /transit/trip/{pattern}/{trip}` | A vehicle's whole journey: every call, its time, and where it is now. |
| `GET /geocode` · `GET /reverse` | Fuzzy place search over the local OSM index, topped up by Nominatim. |
| `POST /share` | Begin sharing a walk. Returns a watch-only link and the walker's write key. |
| `POST /share/{token}/update` · `/end` | Move the dot, or end the share. Needs the write key. |
| `GET /share/{token}` · `GET /s/{token}` | What a recipient sees, as JSON and as a page. |
| `GET /meta` · `GET /health` | Build provenance and liveness. |

Interactive docs at `http://localhost:8000/docs` once running.

```bash
curl -X POST localhost:8000/route -H 'content-type: application/json' -d '{
  "origin":      {"lat": 38.9072, "lon": -77.0369},
  "destination": {"lat": 38.9296, "lon": -77.0324},
  "destinationName": "Columbia Heights",
  "modes": ["walk", "transit"],
  "avoidCameras": false,
  "forceNight": true
}'
```

---

## Transit stops, timetables and live vehicles

The router already holds a full GTFS timetable in memory to plan journeys with.
A departure board is the same data asked a different question — not "how do I
get from A to B" but "what leaves from here, and where does it go" — so the
stop overlay costs no extra memory and no extra build step.

Tapping a stop gives its next departures; tapping a departure gives that
vehicle's whole journey, its position, and its time at every remaining stop.

Three things that had to be got right:

- **A station is not a platform.** WMATA rail publishes two boardable
  platforms per station. Drawn as they come, that is two markers a few metres
  apart with half the departures each. Platforms collapse onto their parent
  station and a board unions them.
- **The map cannot draw eleven thousand bus stops.** Responses are capped, and
  the cap is *reported* rather than hidden — the app says "zoom in for every
  stop" instead of implying that four hundred is all of them. When the cap
  bites, rail survives first, then the busiest interchanges.
- **Midnight.** GTFS times run past 24:00, so a trip leaving at 25:10 is
  yesterday's trip still out on the road. A board reads the schedule twice:
  once as today, once as yesterday shifted back a day. Without the second pass
  the last departures of the night are invisible for the hour before midnight.

### What is live, and what is only claimed to be

Scheduled and live are different claims and the app never blurs them. A
timetable time is what is *meant* to happen; a prediction is what the operator
currently expects. Every row says which it is showing.

The two modes differ because the upstream data does:

| | Position | Per-stop times |
|---|---|---|
| **Bus** | Reported by WMATA — drawn where it is | Schedule shifted by the reported deviation |
| **Metro** | *Estimated*, and labelled as such | Schedule, plus live predictions per station |

WMATA's `TrainPositions` reports a **track-circuit id**, not a coordinate.
Resolving one needs the standard-routes circuit map, a separate feed and a
substantial amount of matching. So a train's position is interpolated between
the station it last left and the one it is next predicted at — accurate to a
few hundred metres mid-run and exact at a platform, which is enough for "is my
train nearly here". It is drawn with a dashed ring and captioned as an estimate
wherever it appears.

Identifying *which* train is the harder half. Rail predictions carry a
`TrainId`; when the departure board matches one to the departure you tapped,
that id is traced across every station on the route and the one reporting the
fewest minutes places the train. Without an id there is nothing to trace, so no
dot is drawn — guessing would put a marker on the map that means nothing.

Predictions are matched to scheduled departures on route and time, not on trip
id, because WMATA's real-time trip ids do not reliably correspond to the static
feed's. The matching is prediction-first: walking the departures and giving
each its nearest prediction lets the 08:00 bus claim a prediction that plainly
belongs to the 08:10 one.

Everything real-time **fails soft**. No `WMATA_API_KEY`, a rate limit, or an
outage degrades the app to scheduled times — which is what it showed before any
of this existed. It must never turn a working timetable into an error page.

> Set `WMATA_API_KEY` to enable live data. Without it the boards still work
> from the timetable and say so.

---

## Working without a server

The person this app is for is out at night, possibly somewhere with no signal.
So the crime overlay is downloadable, and the app is explicit about when it is
using downloaded data.

### What gets downloaded

**Finished hexagons, not incidents.** DC holds well over a hundred thousand
incidents over three years and New York several times that. Shipping those
would mean a large download and re-implementing severity weighting, Gaussian
binning and hex rounding in Swift — three chances to disagree with the server
and produce an overlay that is subtly not the one the routing uses. Instead the
server bins the whole city once per (window, radius) pair and ships the result,
so the offline overlay *is* the online one at the same radius.

Two things keep it small:

- **Only the coarser ladder rungs.** Halving the radius quadruples the cell
  count, and the fine levels only appear when zoomed into a few blocks — the
  view someone with no signal is least likely to need. 375 m and up is baked;
  the app rounds up to the nearest baked level and says so.
- **Centres, not corners.** Twelve floats per cell is several times the rest of
  the record. The pack carries the city's projection origin and the client
  derives the six corners — the same arithmetic, moved to where it is free.

Night-only is deliberately not baked: it would double the file for one toggle,
so the app disables it while offline rather than silently showing the wrong
thing.

Offered on the city picker, because that is where someone is deliberately
setting the app up and the moment to download a few megabytes is while you
still have a connection. The button says *offline crime data*, not "offline
maps": routing needs the whole graph, the search index and the timetable, which
is a different order of size and a promise this cannot keep.

### Knowing the server is gone

Connectivity is judged by **real requests, not by the radio**. A phone can have
four bars and still not reach a backend on someone's laptop, and `NWPathMonitor`
would call that online. Anything that fails as unreachable marks the app
offline; a `/health` poll every fifteen seconds brings it back.

Only `.unreachable` counts. A 404 or a 503 means the server *answered* — the
connection is fine and something else is wrong, and telling someone to check
their Wi-Fi for that wastes their time.

While it is down, a banner sits at the top of the map for as long as the
condition lasts — this is a state, not an event — saying what still works
rather than only what does not, with a manual retry beside the automatic one.

### Downloaded data is a fallback, never a cache

The server is tried **first, every time**, even immediately after a failure.
Downloaded cells go stale as incidents are reported, and preferring them
because connectivity was bad a minute ago would mean showing week-old data to
someone back on Wi-Fi. The fallback only runs once a request has actually
failed.

And when it does run, the app says so: the crime cell sheet is labelled
*downloaded data* in orange. An overlay that is silently a week old is worse
than no overlay.

---

## Sharing a walk

Starting a walk offers to share a live link. Whoever holds it sees where the
walker is and when they expect to arrive, and sees it stop without having to
ask. This is the only part of the system that handles a live human location,
so it is built around what must *not* happen rather than around convenience.

**A share must never outlive the walk.** Someone shares because they are
worried, and a link still broadcasting an hour after they got home is worse
than not offering the feature. It ends four ways, and only one depends on
anybody remembering:

- **arrival**, automatically — the normal case
- **ending navigation**, including by backing out of the screen
- **a hard ceiling** of four hours on the server, whatever the app does
- **silence** — if the phone stops reporting for twelve minutes the share goes
  stale and is then dropped, which covers a dead battery or a killed app

**Reading and writing are separate.** Creating a share returns two secrets: a
token, which goes in the link, and an owner key, which does not. The link
watches and nothing more — it cannot move the dot, extend the share or end it.
The owner key stays in memory on the walker's device and is never persisted.

**Only the current position is held, never a track.** A recipient can see where
someone is; nothing anywhere can say where they have been. When a share ends
the position is dropped immediately, and only the *fact* of arriving survives —
for fifteen minutes, so a friend who looks a moment later sees "arrived" rather
than a dead link.

**The recipient page makes no external requests.** Not even map tiles, and that
is a decision rather than an omission: embedding a tile provider would hand a
third party the live position of someone who never agreed to that. The page
states the position in words and offers to open it in the recipient's own maps
app, which is their choice to make. It is `no-store`, `noindex` and
`no-referrer`, and it says out loud that anyone with the link can see it.

A bad token and a bad key return the same 404, and the page is served for any
token at all — distinguishing them would let someone probing tokens tell a live
share from a dead one.

> **Deployment note.** Shares live in process memory. They do not survive a
> restart, and with more than one worker a share created on one is invisible to
> the others. Fine for a single-process deployment, wrong for anything larger,
> where this wants Redis with the same expiry semantics.

---

## Place search

Search runs against an index of named places built from the same OSM extract
as the routing graph, rather than proxying an external geocoder on every
keystroke. That matters for three reasons: no rate limit, so it can answer as
you type; results come back as a ranked list rather than a single answer; and
we control the matching, so it can be forgiving in the ways that count.

- **Punctuation is ignored.** "Madams Organ" finds Madam's Organ.
- **Abbreviations expand both ways.** "14th st nw" and "14th Street Northwest"
  normalise to the same query, as do Ave/Avenue, Blvd/Boulevard and the
  quadrant suffixes — DC addresses are unusually abbreviation-heavy.
- **Typos still match.** "Dupont Cirle" finds Dupont Circle, via a trigram
  fallback below the exact and prefix tiers.
- **Proximity breaks ties**, which DC needs: there is a 14th Street in more
  than one quadrant.
Matching is tiered rather than one fuzzy ratio, so an exact match always beats
a prefix match, which always beats a merely similar one. A bare similarity
score does not guarantee that and gets embarrassing on short queries.

### Street addresses

A street address is not a name that happens to contain digits, and treating it
as one is why "801 3rd St NW" used to return everything except the building.
The query is **classified first and then matched by a matcher built for that
class**, which is how Maps, Waze and Apple Maps all work.

An address query is decomposed into house number, street name, street type and
quadrant, and matched component by component:

- **The street is a hard requirement.** A doorway on a different street is not
  a worse answer, it is a wrong one, and it is dropped rather than ranked low.
- **The quadrant is close to disqualifying** when it conflicts. This matters
  more in DC than almost anywhere: 3rd Street NW and 3rd Street SE are
  different streets several kilometres apart.
- **The street type is optional.** People type "801 3rd NW" constantly, so
  Street/Avenue/Road is parsed into its own slot and only decides anything
  when a place genuinely has both a Foo Street and a Foo Avenue.
- **Near house numbers are offered next.** Address data is never complete, and
  803 is a useful answer to 801. The score decays over roughly a block, so a
  number four hundred doors away falls off entirely.
- **The street itself is the fallback**, always present and always below any
  real doorway on it.

The house number is detected on the raw text rather than the normalised form,
since normalising collapses "3rd" to "3" and would otherwise read an ordinal
street as a house number.

Nominatim is consulted when the local index returns few results, or when an
address query has not produced the exact doorway — OSM's DC address coverage
is good but not complete. Its results are **merged into the ranking rather
than appended to it**. Appending was the other half of the original bug: the
local index would return four plausible near-misses and push the real answer
to fifth. If Nominatim is down, search degrades rather than breaking.

Recent searches and starred places are stored **on the device only**. Where
someone goes regularly is among the more sensitive things an app can know,
and the server never needs that list to plan a route.

---

## Limitations you should take seriously

This is a safety app, so the ways it can be wrong matter more than usual.

- **Reported crime is not crime.** It reflects what gets reported and where
  police are deployed, both of which vary by neighbourhood in ways that
  correlate with race and income. A low score is not a statement about the
  people who live somewhere.
- **It is historical, not predictive.** Three years of incidents describe
  where crime *has been* recorded. Nothing here forecasts tonight.
- **Absence of data is not safety.** A street with no mapped lamps may be
  perfectly well lit; the inventory has gaps. The same goes for cameras.
- **The weights are opinions.** Whether a robbery should count three times a
  car break-in is a judgement, not a measurement. They are all in one config
  file so you can substitute your own.
- **The safest route is not necessarily safe.** The score is relative to the
  rest of DC.
- **Don't let it override your own judgement.** If a street feels wrong, it
  doesn't matter what the number says.

The UI states several of these at the point of use. If you extend it, please
keep them there.

---

## Layout

```
backend/
  src/getmehome/
    config.py            every tunable weight
    geo.py               projection, polyline maths
    places.py            fuzzy place search index
    daylight.py          solar elevation
    graph/               graph model + build pipeline
    ingest/              OSM, ArcGIS, GTFS readers
    safety/              lighting, crime, cameras, hex grid, scoring
    routing/             A*, alternatives, RAPTOR, multimodal
    nav/                 turn-by-turn instructions
    api/                 FastAPI app
  tests/                 110 tests, no data build required
ios/
  project.yml            XcodeGen spec
  GetMeHome/
    App/                 entry point, settings, planner view model
    Models/              Codable API types
    Services/            HTTP client, location, speech
    Navigation/          route tracker + navigation view model
    Views/               map, route options, navigation, search
  GetMeHomeTests/
```

## Licence and data attribution

Street data © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
Streetlight and crime data from [Open Data DC](https://opendata.dc.gov/).
Transit schedules from [WMATA](https://developer.wmata.com/), subject to their
API licence. ALPR locations from OpenStreetMap contributors.
