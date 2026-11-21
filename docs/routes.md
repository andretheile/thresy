# Routes (`coach route`)

The coach can plan running routes, read route files the athlete sends and turn a past run into a
route file. Routes are GPX files in the workspace's `out/`; the athlete imports them into komoot or
Garmin Connect themselves. Nothing is written to the database or to any account. The contract is
in docs/ARCHITECTURE.md, "Routes"; the code in `thresy.tools.route*`.

| Command | Does | Needs |
|---|---|---|
| `coach route plan` | asks a routing service for candidate routes, writes each as a GPX file (and with `--plot` a picture) | a routing service (below) and the database |
| `coach route show FILE` | the same report for an existing GPX file, e.g. one the athlete planned in komoot and sent (under `inbox/`) | nothing; the database for familiarity and segments |
| `coach route export ACTIVITY` | writes a past activity (id, date or `last`) as a GPX route | the database |

All three print one line in words first, then the numbers, and `--json` gives everything. The
help texts (`coach route plan --help`) are written for the agent and list every threshold.

## What a report contains

- **Distance**, and with a target how far the route is from it. "close to the target" means within
  0.25 km or 2.5 %, whichever is more; otherwise the words say "1.2 km short of" or "2.3 km over"
  the target. The distance is the length of the route itself, never the target.
- **Ascent and descent** from the elevation profile: resampled every 10 m, smoothed over 70 m,
  counted with a 2 m dead band. Planned routes take elevation from the routing service's elevation
  model; `show` from the file; `export` from the watch. A file without elevation gets ascent and
  descent reported as unknown, never estimated.
- **Surface and way type**: the routing service's shares (paved, unpaved, unknown; asphalt,
  gravel, ...; path, track, street, road, ...). GPX files carry no surface; `show` says so.
- **Familiar**: the share of the route on ground the athlete's recorded runs passed before.
  Computed locally (below) and never sent anywhere.
- **Retraced**: the share of the route on ground the route itself covers twice: a point counts
  when the route passes within 20 m of it again at least 100 m further on. Both passes count, so a
  pure there-and-back is 100 %, a loop 0 %, and a 10 km loop with a 2 km "stick" to and from it
  40 %. Two paths closer than 20 m (switchbacks) count as retraced too.
- **Stored segments passed**, in route order, with km from and to, length and gain. A segment
  counts when the route comes within its tolerance (at least 30 m) of its start and then of its
  end, with the distance between inside the segment's stored range (10 % slack). Passing it end
  first is reported as "in reverse" ("downhill" for a climb).

The words: shape "a loop" below 15 % retraced, "with out-and-back parts" from 15 %, "mostly out
and back" from 50 %, "there and back" from 85 % ("point to point" when it ends more than 200 m from
the start); hills by ascent per km: "flat" below 5 m, "gently rolling" from 5, "hilly" from 12,
"very hilly" from 25; ground: "new ground" below 10 % run before, "mostly new" from 10 %, "partly
run before" from 40 %, "mostly run before" from 75 %; surface "mostly paved" or "mostly unpaved"
from 75 %, "mixed" otherwise.

An example on the synthetic data of the tests (invented coordinates):

```
$ coach route plan --via segment:hill --distance 9 --prefer familiar
Route candidates: a loop of about 9 km from the usual start (10.00000, 20.00000); 6 of 8 runs since 2025-11-21 started within 300 m of it
via segment hill (Hill climb)
openrouteservice (foot-walking, detours): 7 request(s), 7 route(s) found, 3 shown; preference: familiar.

In words (from the numbers below):
1. route-9k-b4fe46-1.gpx: a loop, 9.0 km, close to the 9 km target (+0.0 km); gently rolling (50 m up); mostly new ground (23% run before); passes Hill climb (km 1.0-2.0); mixed surface (paved 60%, unpaved 35%, unknown 5%)
2. ...

#  km   vs target  up/down m  familiar  retraced  paved/unpaved/unknown
1  9.0  +0.0 km    50/52      23%       1%        60%/35%/5%
...
Stored segments passed, in route order:
1. km 1.00-2.00 Hill climb (hill), 1.00 km, +20 m
...
```

## Planning

`--start` is `usual` (default), `LAT,LON`, `activity:ID` (an id, a date or `last`; where that
activity started) or a place name. The usual start is where most runs of the last 12 months
started: the first recorded position of each run, clustered on a 150 m grid; the median of the
densest 3x3 block wins. A place name goes through the geocoder of `coach weather`, which is
city-level: it lands in a town centre, not at a door. `--to` takes the same forms (`activity:ID`
is then where the activity ended); without it the route loops back to the start. `--via` (in
order, repeatable) is `LAT,LON` or `segment:KEY`, which runs a stored segment from its start to
its end.

How candidates are made, with at most **10 requests per call** (`--candidates` 1 to 5 are shown):

- **A loop with only a distance**: round trips from the start with seeds 1, 2, 3, ... The service
  treats the length as a wish (its documentation: "a preferred value, but results may be
  different"), so the seeds furthest off are asked again with the length scaled by target /
  actual until enough lie within the tolerance or the requests are used up.
- **Points and no distance**: one route through them; for start and end only, the service's
  alternative routes add up to two more.
- **Points and a distance**: the route through the points first. If it is too short, each
  candidate adds one detour point beside one leg (longest legs first, outward for a loop), placed
  from the missing distance and corrected over up to three requests. The leg inside a stored
  segment never gets a detour. A route already longer than the target stays as it is, and the
  report says by how much it is over.

Candidates are ranked: within the tolerance first, then by `--prefer` (`familiar`: most ground run
before, `new`: least, compared in steps of 10 %), loops before out-and-back shapes, then by the
distance to the target. Near-duplicates (85 % of their ground shared) are dropped. The notes list
what the route could not honour: a start more than 75 m from the nearest path the service knows,
a via point passed at a distance, a segment the service did not run from start to end.

Options that change the route itself, and only these, because the service honours them:
`--trails` uses openrouteservice's `foot-hiking` profile (prefers paths and trails) instead of
`foot-walking`; `--avoid-steps` avoids stairs. openrouteservice also documents "green" and "quiet"
weightings for foot profiles. They rest on extra data prepared when the routing graph is built,
and its documentation does not say where the hosted service has that data, so they are not
offered: an option that may silently do nothing would mislead the coach.

Files: `out/route-<km>k-<hash>-<n>.gpx` (the hash comes from the request, so asking again
overwrites, and a different request gets new names), pictures `.png` next to them.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `COACH_ROUTE_PROVIDER` | `openrouteservice` | `openrouteservice` (or `ors`), or `none` to switch planning off |
| `COACH_ROUTE_URL` | the hosted service, `https://api.heigit.org/openrouteservice` | base URL of the routing service, the part before `/v2`; a self-hosted instance needs no key |
| `COACH_ROUTE_API_KEY` | unset | the openrouteservice key, sent in the `Authorization` header (never in a URL), kept out of logs and error texts |
| `COACH_ROUTE_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | map tiles behind the pictures; `none` switches them off |
| `COACH_ROUTE_TILE_ATTRIBUTION` | `© OpenStreetMap contributors` | the credit line drawn on pictures with tiles; set it when another tile server requires its own |
| `COACH_ROUTE_TILE_CACHE_DIR` | `$XDG_CACHE_HOME/thresy/tiles`, else `~/.cache/thresy/tiles` | tile cache (in the deployment: the agent's home volume) |

The bridge hands the set values to the agent (`agent_env`). This routing key is the one kind of
key the agent may hold (docs/ARCHITECTURE.md, principle 7): it grants nothing but queries of a
public data service, so take it from a free account used for nothing else.

### A free openrouteservice key

1. Create an account at <https://account.heigit.org/> (openrouteservice is run by HeiGIT; the
   account replaced the old openrouteservice.org developer dashboard).
2. In the dashboard, request a token on the free Standard plan. The key is shown there; quotas
   per day and per minute are shown next to it.
3. Put it into `COACH_ROUTE_API_KEY` (docs/deploy.md, "Routes", has the safer place on a server).
4. Check it with the command under "Checking a key" below.

The quota of the free plan is enough for a coach: one `coach route plan` sends at most 10
requests. When the daily quota is used up the service answers 403 and the command says so in one
line (the quota renews 24 hours after the day's first request); the per-minute limit answers 429.
Repeatedly exceeding the limits gets a key blocked for a while.

### Self-hosting

openrouteservice can run as your own container (<https://giscience.github.io/openrouteservice/>,
"Running with Docker"), built from an OpenStreetMap extract of your region; it then needs no key
and no request leaves your network. Set `COACH_ROUTE_URL` to its base URL, the part before `/v2`,
for example `http://ors:8082/ors` when it runs as service `ors` on a network the bridge shares.
Building the graph for a region needs several GB of memory and disk, which is why the Thresy
stack does not include it. Enable the `foot-walking` profile (and `foot-hiking` for `--trails`)
and elevation in its configuration.

## What leaves the machine

- **Routing request** (`plan` only): the coordinates of the start, the via points (a segment's
  start and end), the end and any detour points, plus the options (profile, round-trip length and
  seed). The usual start is often the athlete's home. Nothing else: no history, no names, no
  activity ids, no dates.
- **Map tiles** (`--plot` with tiles on): the tile server sees which tiles of which area are
  fetched, and the client's IP address. `COACH_ROUTE_TILE_URL=none` sends nothing.
- **Geocoding** (`--start` with a place name): the name goes to Open-Meteo, as with
  `coach weather`.
- Familiarity, segments and the usual start are computed in the database and never sent.

## Map tiles and the OpenStreetMap tile policy

The standard tiles of `tile.openstreetmap.org` are run on donated resources and governed by the
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/). It welcomes light use
by applications that identify themselves, and forbids bulk downloading, offline use and prefetching.
`coach route` draws one picture of the area a route covers when the coach asks for it, which is
the use the policy permits ("Normal interactive viewing ... where the client requests only the
tiles needed for the current viewport"), so it uses them by default and complies:

- an identifying User-Agent, `Thresy/<version> (self-hosted running coach)`, with no personal data;
- the exact URL the policy names, over HTTPS;
- at most 16 tiles per picture (the zoom is lowered until the route fits), only the tiles the
  picture shows, fetched one after the other;
- an on-disk cache that honours `Cache-Control` and `Expires` (7 days when the server sends
  neither), and conditional requests (`If-None-Match`, `If-Modified-Since`) once a tile has
  expired; never no-cache headers;
- "© OpenStreetMap contributors" drawn on the map, bottom right;
- when the server refuses (401, 403, 418, 429) or cannot be reached, no further tile is asked for
  in that call, and the picture is drawn without a map background.

Every deployment sends its own requests, so heavy use anywhere is the operator's to avoid. If you
cannot meet the policy, or the coach makes many pictures, set `COACH_ROUTE_TILE_URL` to another
OSM-derived tile service (with its key in the URL if it needs one, and its credit in
`COACH_ROUTE_TILE_ATTRIBUTION`) or to `none`. Without tiles the picture still shows the route,
kilometre marks, direction, start and finish, the stored segments, the athlete's known ground, a
scale bar and the elevation profile.

## Attribution

- Route data from openrouteservice: "© openrouteservice.org by HeiGIT | Map data ©
  OpenStreetMap contributors", printed with every plan and drawn on its pictures. openrouteservice
  results are licensed CC BY 4.0, and the routes are computed on OpenStreetMap data (ODbL).
- Map tiles: "© OpenStreetMap contributors" (or the configured line), drawn on the map.
- Elevation of planned routes comes from openrouteservice's elevation model.

## History in the database

Known ground is computed in SQL: the positions of runs (not rides) inside the box the routes (with
`--plot`, the pictures) cover, plus 150 m, reduced to distinct cells of 30 m. Only the cells come
back to Python, never the samples, because the agent shares the bridge's memory limit. A point of
a route is familiar when its cell or one of the eight around it is known. The query runs with a
30 s statement timeout, and with `work_mem` 32 MB and JIT off for its transaction.

Measured on the development Postgres (Postgres 16, default 128 MB shared buffers, two parallel
workers; Apple-silicon laptop) with generated data of the production size: 1,040 activities,
2,899,520 samples, `activity_records` 395 MB, every activity a loop near one point, so the box of
a 10 km loop holds 823,000 samples (about 28 %):

| Query | Plan | Time |
|---|---|---|
| known ground, 10 km loop, first run | sequential scan, hash join to `activities`, de-duplication | 3.9 s (157 MB read into the cache) |
| known ground, warm, default settings | parallel sequential scan, sort spilling about 13 MB to disk, JIT | 0.6-1.4 s |
| known ground, warm, as configured | sequential scan, hash aggregate in memory (25 MB) | 0.4-0.6 s |
| usual start (910 runs in 12 months) | one primary-key index lookup per run | 0.08 s |
| `coach route show` of a 10 km loop, whole command | | 1.2 s; with `--plot` 1.7-2.4 s, 180 MB peak |

The known-ground query reads the whole samples table: there is no index on positions, and an
index would not help much because nearly all samples lie near home. On a server where the
table does not fit the database container's memory (512 MB limit, 128 MB shared buffers) every
call is closer to the first-run figure. What would make it independent of the table's size is a
small cell table filled at import and sync time, for example
`activity_cells(activity_id, cy int, cx int, PRIMARY KEY (cy, cx, activity_id))` on a fixed
global grid (cells of 0.00027° latitude by 0.00027°/cos(latitude band) longitude, about 30 m):
roughly 300,000 rows instead of 2.9 million samples, read by an index range scan in a few
milliseconds. That needs a migration and changes in `thresy.store`, so it is not part of this
package.

## Checking a key

```sh
COACH_ROUTE_API_KEY=<key> COACH_WORKSPACE_DIR=/tmp/route-check COACH_ROUTE_TILE_URL=none \
  uv run coach route plan --start <LAT,LON> --distance 5 --candidates 2
```

Use the coordinates of a public place, such as a town square, not a home. It should print two
loops of about 5 km with ascent and surface shares, and files under
`/tmp/route-check/out/`. Add `--plot` and drop `COACH_ROUTE_TILE_URL=none` to try the tiles.

## Limits

- Routes follow map data. Paths can be closed, private, unlit, seasonal or impassable, and the
  coach says so when it offers a route; the athlete should look at it before running it.
- Round trips and alternative routes are limited to 100 km by the hosted service, routes to 50
  points. `--distance` takes 0.5 to 100 km.
- A detour point that lands in water or a field is moved to the nearest path within 1 km by the
  service, which can add a short out-and-back spur: the retraced share shows it.
- The usual start is the median of start positions, not an address; the service moves it to the
  nearest path (the notes say when that is more than 75 m).
- Familiarity counts runs only, at a resolution of about 30 to 90 m: a parallel street 50 m away
  can count as run before.
- No Garmin course, no komoot or Strava account: sending a course to Garmin would be an
  `agent_requests` kind with confirmation (docs/ARCHITECTURE.md, "Routes").
