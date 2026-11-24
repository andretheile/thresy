# Analysis

`coach analysis …` turns the per-second records, laps and wellness data in Postgres into the
numbers a coach needs: splits, interval reps, segment histories, course comparisons, the
gradients of courses, heart-rate plausibility, the HRV curve of a night, wellness trends and
weekly volume. The commands are thin wrappers around library functions in `thresy.analysis` that work on pandas frames, so an
agent can also import them for questions no command covers.

Conventions shared by every command:

- **Selecting an activity.** The `ACTIVITY` argument is an activity id, `last`, or a local date
  `YYYY-MM-DD` (the longest run that day; the others are listed). The options `--last` and
  `--date` do the same. Without a selector the most recent run with per-second data is used.
  `coach analysis list` finds ids by date, name or type. Commands that need per-second data
  only consider activities that have it.
- **Output.** Readable text by default; `--json` prints everything, including values the text
  leaves out. JSON uses seconds, metres, bpm and seconds per km; local times are naive ISO
  strings in the activity's (or the night's) own time zone.
- **Plots.** `--plot` writes a PNG to `COACH_WORKSPACE_DIR/out/<name>.png` and prints
  `plot: <path>` (`"plot"` in JSON). Names are deterministic, so a new plot of the same thing
  replaces the old file. Axis labels follow `COACH_LANGUAGE` (`en`, `de`).
- **Database.** Commands read through `COACH_DATABASE_URL`, which in agent runs is the
  `coach_agent` role. Only the segment commands write (`segments`, `segment_efforts`), and they
  fall back to computing without the cache when the connection may not write.
- **Times.** Timer time (watch pauses excluded) unless a field says `elapsed`. A gap of more
  than 10 s between two records counts as a pause. Standing still means speed below 0.5 m/s;
  averages of cadence and power use samples at 1 m/s or faster.

## Commands

### `list`

```sh
coach analysis list --days 14
coach analysis list --name tempo --limit 50 --json
coach analysis list --type track_running --with-records
```

Id, local start, type, name, distance, moving time, pace, average heart rate, ascent and
whether per-second data exists. Runs only unless `--type` or `--all-types` is given.

### `activity`

```sh
coach analysis activity                      # the last run
coach analysis activity 2030-05-01 --plot
coach analysis activity 123456 --split-m 500 --min-climb-m 15 --descents --json
```

- **Splits** every `--split-m` metres (default 1000). Time is timer time between the moments
  the distance was first reached (so splits add up to the total), pace from that time, average
  and maximum heart rate, ascent and descent, cadence, and time stopped inside the split
  (standing or paused).
- **Ascent and descent** use the altitude smoothed with a centred mean over 15 samples and a
  1 m dead band: a change counts once the altitude has moved 1 m from the last counted point.
  On barometric data this lands within a few metres of Garmin's own figure.
- **Halves** split the run at half the distance and compare pace, heart rate and ascent.
- **Heart-rate drift.** The first 10 minutes of timer time are skipped (warm-up); the moving
  samples after that are split into two halves of equal timer time. For each half: mean heart
  rate, pace (distance / timer time) and the efficiency factor EF = speed in m/min divided by
  heart rate (metres per beat). `drift_bpm` = HR(second) − HR(first); `decoupling_pct`
  = (EF₁ − EF₂) / EF₁ × 100 (Pa:HR). Below about 5 % the pace was held without the heart rate
  creeping up. Needs at least 20 minutes after the skipped warm-up.
- **Climbs** come from the smoothed altitude cut at its turning points (a zigzag with 10 m
  hysteresis: a climb ends once the altitude has fallen 10 m below its highest point). A climb
  starts at the last sample within 0.5 m of its lowest point (so a flat approach is not part
  of it) and ends at the first sample of its top. Listed are climbs with at least
  `--min-climb-m` (default 25 m) net gain and 2 % average grade, with distance, gain, grade,
  time, pace, VAM (metres climbed per hour), heart rate and power. `--descents` adds descents.
- **Stops**: total paused time (gaps between records) and standing time; stops of 20 s or more
  are listed with their position and local time.

The plot shows pace, heart rate, altitude and cadence over distance with the climbs shaded.

### `intervals`

```sh
coach analysis intervals --last --plot
coach analysis intervals 123456 --source records --signal power   # hill repeats
```

Repetitions are found in this order (`--source auto`):

1. **Workout laps.** Laps carry a workout step and their intensity names both work
   (`active`/`interval`) and rest (`rest`/`recovery`) steps: work laps are the reps.
2. **Lap presses.** At least three laps were ended with the lap button. The laps ended by a
   press, plus the lap after each press, are split into a fast and a slow group by average power
   (when all of them have power) or speed, using the split with the largest between-group
   variance (two-means). Accepted with at least two work laps, the fast group at least 1.15
   times the slow group, and only pressed laps between the first and the last rep (a few stray
   presses in an auto-lap run do not count). Slow laps before the first and after the last rep
   are warm-up and cool-down.
3. **Records.** The per-second speed (or power with `--signal power`) is smoothed with a
   centred 5 s median and a 5 s mean and split into fast and slow by two-means over the moving
   samples. Accepted when the two groups are clearly separated (Ashman's D ≥ 3 and ratio ≥
   1.15); work is every stretch above the threshold, dips under 10 s are bridged and stretches
   under `--min-work-s` (default 15 s) dropped. The result is rejected when the median break
   between the stretches is shorter than 20 s, or when the reps do not end with a heart rate at
   least 5 bpm above the end of the recoveries (mean of the last 30 s of each rep against the
   last 15 s of the recovery after it): the fast downhills of a hilly easy run look like reps
   by speed but lower the heart rate. Use power for hill repeats: it rises uphill where speed
   falls (but it also flags uphill jogging in a warm-up).

Per rep, from the records inside the rep: timer time, distance, pace, average and maximum heart
rate, heart rate over the **last 30 s**, heart rate at the end (last 5 s), **drop in the first
60 s of recovery** (end value minus the mean of 57–63 s after the rep; it is measured even when
the recovery is shorter, so check `recovery_s`), recovery length (to the next rep), cadence,
**stride length** (speed / cadence, metres per step), breathing rate (mean/max, when the watch
records it) and power. Recoveries get time, distance, pace, average and minimum heart rate. The
summary averages the reps (pace from total time and distance) and gives the change in average
heart rate from the first to the last rep.

The plot shows pace and heart rate of the session with the reps shaded, and the heart rate of
every rep against time since its start (light = first rep, dark = last).

### `intervals-compare`

```sh
coach analysis intervals-compare --rep-m 1000 --first 4 --plot
coach analysis intervals-compare --rep-s 180 --since 2030-01-01
```

Analyses every run with per-second data in the date range and lists the sessions with at
least `--min-reps` (default 3) reps within `--tolerance` (default 15 %) of the given distance
or duration. Other reps of a session (a final 200 m, say) are ignored. `--first N` averages
only the first N matching reps, so a 4-rep session compares fairly with an 8-rep one. To keep
the scan fast, only runs with workout steps or at least three lap presses, or whose moving
speed varies (coefficient of variation ≥ 0.10), are analysed.

### `sensor`

```sh
coach analysis sensor --last
coach analysis sensor 123456 --plot --json
```

Can the heart rate be trusted?

- **Cadence lock** (the wrist sensor reports the step rate as heart rate). A sample is
  cadence-like when cadence ≥ 100 spm and |HR − cadence| ≤ 3. Stretches where at least 75 %
  of the samples in a centred 120 s window are cadence-like, lasting at least 2 minutes, are
  reported (and extended to the adjacent cadence-like samples). Confidence is `high` when heart
  rate follows cadence (Pearson r ≥ 0.6), matches it tightly (mean |HR − cadence| ≤ 1.5) or
  jumps by ≥ 20 bpm within 15 s at an edge; otherwise `possible` (a hard effort can bring the
  two together by coincidence).
- **Jumps**: a change of ≥ 20 bpm between samples at most 3 s apart.
- **Spikes**: ≥ 20 bpm above the centred 61 s median for at most 30 s (a false maximum).
- **Dropouts**: no heart rate for ≥ 30 s while moving.
- **Level shifts**: the median heart rate of the 30 samples just after a moment differs by
  ≥ 30 bpm from the 30 samples just before it (10 samples on either side left out; only
  samples with heart rate count, so a dropout of up to 5 minutes in between is bridged), while
  the effort (median power, else speed) of the two windows differs by at most 10 %, the effort
  over the surrounding 240 samples is steady (coefficient of variation ≤ 0.2), and the watch
  was neither paused nor standing in between. A heart cannot move that far within a minute or
  two at the same effort, so one side is wrong: typically a wrist sensor that read the step rate
  and then recovered, or the other way round. For both sides the share of samples within 5 bpm
  of the cadence over 10 minutes is given; a side near 1 is the one reading the step rate. The
  first 5 minutes are not checked.

Verdict: `unreliable` with a high-confidence lock of 5 minutes or more, a level shift, or more
than 15 % of the moving time flagged; `suspect` when anything else is reported; `none` without
heart rate; otherwise `ok`. The average and maximum without flagged samples (high-confidence
locks and spikes) are given as well; after a level shift, use pace and power instead.

### `route find` and `route compare`

```sh
coach analysis route find 123456              # earlier runs on the same course
coach analysis route compare 111111 123456 --plot
```

`route find` scores other activities whose track lies near the reference: `coverage` is the
share of the reference track (sampled every 5 s) within about 40 m of the other track, and
`on_course` the share of the other track that lies on the reference (low when the other run is
much longer). Default minimum coverage 0.8.

`route compare A B` treats A's GPS track as the course. Each sample of B is projected onto the
nearest point of A's track within 40 m, searching only 100 m behind to 400 m ahead of B's
progress so far, so out-and-back courses and loops are followed in order; progress never
decreases. For each course distance x, the elapsed time at which each run first reached x gives
`time(x)`; the comparison covers the common part of the course and measures both times from
its start. `delta = time_B − time_A` (positive: B is behind). Stops are included because they
cost time; a stop counts in the section that starts where it happened. The output gives the
totals, heart rate and stopped time of both, a per-section table (`--step-m`, default 1 km)
with the course's ascent, both times, the difference and the cumulative difference, the climbs
on the course (from A's altitude) with both times, and the sections where B lost the most.

### `grades`

```sh
coach analysis grades out/race.gpx 123456 last           # a race course against two runs
coach analysis grades 123456@3-20.1 inbox/2030-03-04/route.gpx --labels Race,Planned --plot
coach analysis grades race.gpx 2030-05-01 --share --plot --json
```

How hilly is a course, and how does it climb? It answers questions such as "is the race course
hillier than my long-run loop?" or "which of my courses is most like the race?" from the
gradient of every 100 m piece, for one to four courses side by side. A course is an activity
(an id, a date or `last`, as everywhere) or a GPX file: a route from `coach route`, a file the
athlete sent, a race course from the organiser. `@FROM-TO` (km) cuts a course to a stretch;
either end may be left out (`last@5-`). The first course is the reference: every later one is
compared with it.

**Method.** Each course is cut into pieces of `--bin-m` (default 100 m) from the start of its
stretch; a last piece shorter than half a piece is dropped. The altitude at a piece's edge is
the altitude where the distance first reaches that edge, so standing still and watch pauses
create no piece and no gradient. Gradient = altitude change over the piece / its length, rounded
to 0.1 % before it is classed, so a piece shown as 5.0 % counts as steep.

- **Activities** use the watch's distance and its altitude smoothed like everywhere in the
  analysis (centred mean over 15 samples, about 45 m at running pace).
- **GPX files** have positions and elevation but no per-second samples. The distance is the
  path length along the positions (repeated points dropped). The elevation is resampled every
  10 m along that distance and smoothed with a centred mean over 70 m, narrowing near both
  ends so the window stays centred (a straight slope stays straight to the last point). Points
  in a file are spaced by distance, often unevenly (a router puts few points on a straight
  road), so the smoothing works on distance, not on point counts. A route's elevation usually
  comes from a terrain model sampled every 30 m or so, which arrives as small steps and spikes
  at bridges and embankments; 70 m removes those without flattening a 100 m ramp much, is about
  the distance the watch's 15 s cover, and is the window `coach route` uses for its ascent, so
  both commands see a file the same way. A file without elevation is refused; points without
  elevation inside a file are interpolated and counted in the output.

The output names each course's elevation source. When both kinds are compared it adds a note:
watch altitude (barometric, smooth, drifting with the weather) and a terrain model (misses
short ramps, steps at bridges) differ in detail, so compare the bands and classes, not single
pieces, and expect a few percent of difference in ascent from that alone.

**Numbers per course.**

- Ascent and descent summed over the pieces. Ups and downs inside one piece cancel, so this is
  lower than the watch's own total, which an activity also shows (Garmin's figure for the whole
  activity, even when a stretch was asked for). Ascent per km is the sum over the length.
- Five **bands**, in km and in % of the course's length (courses differ in length): steep
  down (≤ −`--steep-pct`), gentle down, flat (below `--flat-pct` either way), gentle up, steep
  up (≥ `--steep-pct`); defaults 1 % and 5 %. Uphill km = gentle up + steep up.
- **Classes** `--class-pct` wide (default 2 %), centred on 0, ±2, ±4 …: the class around 0 is
  −1 to +1, the flat band. A gradient of exactly +1 % belongs to the class above, −1 % to the
  one below, symmetrically. The outermost classes collect everything beyond `--class-limit`
  minus half a class (with the defaults: from 9 % either way). The text leaves out empty outer
  classes; JSON has all of them.
- The **steepest piece** up and down with its km.
- **Climbs.** The altitude at the piece edges is cut at its turning points with the zigzag of
  `activity` (`thresy.analysis.activity.turning_points`), but with 5 m instead of 10 m: a climb
  ends once the altitude has fallen 5 m below its top, so a short dip does not split a long
  climb while the small rollers this command should tell apart stay separate. Pieces below the
  flat limit at either end are trimmed off. A climb counts with at least `--min-climb-m`
  (default 10 m) gain and an average gradient of at least the flat limit. Listed: km from and
  to, length, gain, average gradient and steepest piece; the text shows the `--climbs`
  (default 5) largest by gain, in course order; `climbs_found` counts all.
- **Steep stretches**: runs of consecutive steep pieces, up and down, with km, length, average
  and steepest piece. Those shorter than 500 m are the **short steep ramps**.

**Likeness of two courses** is the overlap of their distributions of distance over the classes,
each as a share of its own length: the sum over the classes of the smaller of the two shares,
0 to 100 %. 100 % means the same mix of gradients (not the same order along the course); 0 %
means no class holds distance of both, as between a course that only climbs and one that only
descends. It depends on the class width: with narrow classes, two courses at 2.9 % and 3.1 %
can fall into different classes.

**Words.** Ahead of the numbers, one line per course and one per later course against the
first, built with fixed thresholds (also in `--help` and in `thresy.analysis.grades`):

- hilliness from ascent per km over the pieces: `flat` below 5 m, `gently rolling` from 5,
  `hilly` from 12, `very hilly` from 25 (the words and limits of `coach route`, which computes
  its ascent with a dead band instead, so a course near a limit can get the next lower word
  here);
- the uphill km: `all of it gentle`, `mostly gentle` when under 25 % of them are steep,
  `partly steep` from 25 %, `mostly steep` from 50 %, `all of it steep`, with both km;
- the short steep ramps with their km and the steepest piece among them; longer steep stretches
  with km, length and gradient;
- the climbs: `mostly long` when climbs of 1 km or more hold at least half of all the climbs'
  gain, `mostly short` when climbs under 0.5 km do, otherwise `of mixed length`; `steep` when
  those climbs average the steep limit or more, else `gentle`;
- against the first course: `more` or `less climbing per km` when the larger value is at least
  1.25 times the smaller and 2 m/km more, otherwise `about as much`; `more` or `less steep
  uphill` when the steep-up share of the distance differs by 2 points or more; both courses'
  number of short steep ramps; the likeness, `very alike` from 85 %, `alike` from 70 %,
  `partly alike` from 50 %, else `different`.

JSON has everything, including the gradient of every piece (`piece_from_km`,
`piece_grade_pct`) and the likeness of every pair.

The plot shows one panel per course with a bar per piece (gradient along the course km; every
panel the same gradient range and the same km per inch; steep gradients shaded), and below them
the distance per class, the courses side by side in the same colours. `--share` shows that
panel in % of each course's length, which compares courses of different length fairly.

The functions work on distance and altitude arrays (`grades.cut`, `grades.measure`,
`grades.compare`), so other code can measure a course it has in hand, such as a planned route.

### `segments`

```sh
coach analysis segments import private/segments.toml
coach analysis segments list
coach analysis segments history hill --plot
coach analysis segments history hill --hr-band 145-155 --since 2030-01-01
coach analysis segments match --last
```

A segment is a start and an end point; definitions belong to the athlete and live in the
database, loaded from a file kept out of git. TOML:

```toml
[[segments]]
key = "hill"                 # used on the command line
name = "Hill from the bridge"
start = [10.0001, 20.0001]   # lat, lon (made-up example)
end = [10.0101, 20.0021]
tol_m = 30                   # how close a track must pass the points (default 30)
dist_min_m = 800             # accepted effort distance, exclusive (optional)
dist_max_m = 1200
max_s = 1800                 # end must be reached within this time (default 1800)
uphill = true                # must end higher than it starts (default true)
```

JSON works as well (a list of such objects, or `{"segments": [...]}`). Re-importing a changed
definition clears that segment's cached efforts.

Matching is the old bot's matcher applied to database records: an effort starts at the last
sample within `tol_m` of the start point before the first sample within `tol_m` of the end
point, reached within `max_s`; it counts when its distance lies strictly between the limits,
it lasts more than 30 s and, for uphill segments, ends higher than it starts. Distances to the
points use an equirectangular approximation; durations are wall-clock time.

`history` lists every effort with date, time, distance, net gain, pace, average and maximum
heart rate, heart rate over the last 60 s, metres per heartbeat (distance / (average HR ×
minutes)) and **pace at heart rate**: a line pace = a + b · HR is fitted over all efforts
(needs at least 5 efforts spanning 8 bpm, and b < 0), and each effort's pace is moved along
that slope to a common heart rate (`--at-hr`, default the median). This answers "am I faster
at the same heart rate than last year?" when efforts were run at different intensities; a
heart-rate band (`--hr-band`) is the fit-free alternative. Also the best effort and a per-year
summary. The plot shows segment time over date (colour = average heart rate) and average heart
rate against pace, coloured by year.

Efforts are cached in `segment_efforts`. Candidates are found with one bounding-box query
(activities with records near both points); activities with cached efforts are not scanned
again unless `--refresh` is given (`--no-cache` neither reads nor writes the cache). On a
read-only connection the efforts are computed and the output says `cache read-only`.

### `night`

```sh
coach analysis night                    # last night: wake date = today
coach analysis night --date 2030-05-02 --plot
```

Nights are keyed by **wake date**: `--date 2030-05-02` is the night that ended on the morning
of 2 May. When that night has not been synced yet the command says so and names the latest
synced night instead of silently showing an older one.

Stages come from `sleep_levels` with Garmin's activityLevel mapped as 0 = deep, 1 = light,
2 = REM, 3 = awake (the old bot treated 2 as awake, which is wrong). Each HRV reading (about
every 5 minutes) gets the stage it falls into. Reported: sleep window, duration, score and
stage totals; Garmin's night average, 7-day average, status and balanced range; resting heart
rate; and from the readings: the average over all readings, the average **while asleep**
(readings in awake periods left out), the average over the **first 3 hours** after sleep start
(awake readings left out), minimum and maximum with their times, the average per stage, and
the awake periods (consecutive awake levels merged) with start, end and duration. Local times
use the offset Garmin reports for the night, falling back to `COACH_TZ`.

### `wellness`

```sh
coach analysis wellness --days 28 --plot
```

Per wake date: HRV night average against the balanced range (flagged below or above),
7-day average and status, resting heart rate, sleep duration and score, steps. The summary
compares the last 7 days with the 7 before and counts nights below the range and days without
sleep data.

### `weeks`

```sh
coach analysis weeks --weeks 12
```

Monday-to-Sunday weeks by local date: number of runs, distance, time (moving time, else timer
duration), ascent (Garmin's elevation gain) and longest run; the current week is partial. Then
the year to date against the same period last year. `--all-types` counts every activity.

## Using the library

```python
from thresy import db
from thresy.analysis import data, intervals

with db.connect() as conn:
    sel = data.resolve_activity(conn, "last")
    df = data.load_records(conn, sel.activity.id)          # pandas, see thresy.analysis.data
    laps = data.load_laps(conn, sel.activity.id, df["ts"].iloc[0])
    result = intervals.analyze(sel.activity, df, laps)
```

Record frames have short column names (`t`, `dist`, `speed`, `hr`, `cad`, `power`, `resp`,
`alt`, `lat`, `lon`) plus derived `timer`, `paused`, `still`, `still_s` and `stop_s`; the module
docstring of `thresy.analysis.data` defines them.

## Known limits

- Everything that needs per-second data only covers activities with a FIT file; older
  activities imported from summaries alone show up in `list` and `weeks` but not in
  `intervals-compare` or segment histories.
- Interval detection from records assumes reps are faster than recoveries. Hill repeats need
  `--signal power` (or `activity` climbs), and a hilly warm-up can then add spurious reps. Edges
  found from records trail the athlete's lap presses by about 2–7 s (acceleration takes a few
  seconds); laps are preferred when they exist.
- HR drift and decoupling are distorted by hills and by stops; compare the reported ascent of
  the halves before reading much into them.
- Cadence lock can only be told apart from a coincidence when cadence varies or the heart rate
  jumps at the edges; long locks at a constant cadence are reported as `possible`.
- Route comparison needs both runs to follow the same course closely (within 40 m); a run that
  joins the course late is compared on the common part only. Altitude for the course comes from
  the reference run.
- Gradients over 100 m pieces smooth out what is shorter: a 50 m ramp at 15 % shows as a
  piece of at most 7.5 %, less when it straddles two pieces. Use `--bin-m 50` to look closer, at the price of more noise from the
  altitude. Watch altitude and a file's elevation differ in detail (see `grades`), and a
  watch's barometer drifts in changing weather, so two runs of the same course differ by a few
  percent in ascent.
- Segment matching follows the old matcher closely, including its equirectangular distance and
  wall-clock durations, so efforts with a watch pause inside are longer than the timer time.
- The night analysis depends on Garmin's HRV readings, which are missing on some nights.
