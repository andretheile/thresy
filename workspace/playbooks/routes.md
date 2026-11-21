# Routes: planning one, reading one

A good route lets the athlete run the session as planned: the right distance, a shape and terrain that suit the session, and no surprises they could have been warned about. You hand it over as a picture and a GPX file; the athlete imports the file into komoot or Garmin Connect. Read `coach route plan --help` the first time in a session: it lists what every word in the report means.

## When to offer one

When it adds something: a long run longer than their usual loops, a session that needs a particular climb or a flat stretch, a run somewhere unfamiliar (a trip, a race venue), or when the athlete says they are tired of the same loops. Not every day, and not when they have their own plan for the run.

## Planning

1. **What does the session need?** Distance from the plan (`coach plan show`), hills or flat, surface (after heavy rain unpaved means mud: `coach weather`), daylight (in the dark prefer ground run before), familiar ground for a key session or new ground for an easy one (`--prefer`). The start is the usual one unless the athlete runs from elsewhere (memory, `places.md`); a place name lands in a town centre, so prefer coordinates.
2. **Plan:** `coach route plan --distance 12 --plot`, with `--via segment:KEY` for a stored climb, `--trails` for paths over roads.
3. **Choose:** read each candidate's line of words first, then the numbers. Close to the target; a real loop for a long run unless there and back is wanted (flat tempo, easy turnaround); the hills the session wants; the notes: a start moved to a path, a segment not run as asked, a route over the target because the points are far apart. If none fits, change the request (another `--via`, `--prefer`, a different distance) rather than sending a poor one.
4. **Send one** route: `coach send photo out/<name>.png` and `coach send document out/<name>.gpx`, then two or three lines: why this one, distance and climbing, where the key part is. No table of all candidates; mention an alternative only if it is a real choice ("a flatter one is ready").
5. **Be honest about the map.** The route follows map data: a path can be closed, private, unlit, muddy or a busy road without a pavement. Ask the athlete to look at it before running it, especially somewhere new or in the dark.

## A segment inside a session

- `--via segment:KEY` runs the stored climb from its start to its end; the report says where it starts and ends (km from, km to). Hard efforts on it need a warm-up first, about 2 to 3 km easy: `--warmup 3` puts the segment at least 3 km in, and a note says when that was not possible.
- Give a target from the athlete's own efforts on it: `coach analysis segments history KEY` (best, recent ones, pace at a common heart rate), not a generic one.
- Write it into the plan with the place in km: `coach plan add sun "Long run with Ridge" -d "6 km easy, Ridge from km 6.4 at threshold effort, easy home"`.

## A route the athlete sent

Files from the chat are under `inbox/<date>/`. `coach route show inbox/<date>/<file>.gpx --plot` gives the same report. Say what matters for the planned session: distance against the plan, where the climbing is, how much is out and back, what is new ground. A file without elevation gets ascent reported as unknown: say so instead of guessing. A screenshot of a route has no numbers you can rely on: ask for the GPX export.

## A past run as a route

`coach route export ID` (or a date, or `last`) writes `out/activity-<id>.gpx` to run a course again or to share it; send it with `coach send document`.

## Shape

Illustrative only, with invented numbers:

> Here is a 12.3 km loop for Sunday: 140 m of climbing, two thirds on paths you know. The Ridge comes at km 6.4 to 7.6, so you are warm by then; your best there is 5:12. The last 2 km follow the river. Have a look at it on the map before you go: I cannot see whether a path is closed.
