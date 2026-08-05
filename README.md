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
| Backend routing, safety model, transit, crime grid, search, API | Written and covered by 105 passing tests |
| `RouteTracker` navigation maths | Algorithm validated independently against hand-computed cases |
| iOS app | Compiles and launches; UI beyond that not exercised here |
| The DC data build (`make graph`) | Streetlight + crime ingestion fixed against the real feeds |

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

Lighting carries **zero weight in daylight**, switched on real solar elevation
rather than a clock hour — DC sunset moves by nearly three hours across the
year.

### Crime

MPD incidents over three years, as a rasterised Gaussian KDE. Each incident is
weighted by:

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
- **recency** — exponential decay with a 400-day half-life

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
cd ios && xcodegen generate && open GetMeHome.xcodeproj
```

Set your development team in the target's Signing & Capabilities tab, then
build to a simulator or device.

On the **simulator**, the default server URL (`http://localhost:8000`) works as
is. On a **physical device**, localhost is the phone — open Settings inside the
app and point it at your Mac's LAN address, e.g. `http://192.168.1.42:8000`.

Without a paid Apple Developer account you can still run on a device, but the
provisioning profile expires every 7 days and you will need to re-install.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /route` | Plan itineraries. Body takes origin, destination, modes, `avoidCameras`, optional `departAt` and `forceNight`. |
| `GET /cameras` | Flock/ALPR cameras in a bbox, for the overlay. |
| `GET /crime/grid` | Incidents binned into hexagons over a bbox, for the map overlay. |
| `GET /geocode` · `GET /reverse` | Fuzzy place search over the local OSM index, topped up by Nominatim. |
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

Nominatim is consulted only when the local index returns few results, mostly
for house-number addresses that OSM carries as interpolation rather than as
named objects. If it is down, search degrades rather than breaking.

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
  tests/                 105 tests, no data build required
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
