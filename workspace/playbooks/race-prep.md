# Races: deciding, preparing, racing, recovering

## A race comes up

- **Check the facts** where you can read the web: date, start time, distance, ascent, course and surface, entry status and cut-offs, on the organiser's page. Enter the race with `coach races add` (with `--url`, and `--status maybe` while undecided). If what you found differs from what the athlete said (another date, another distance), say so before you plan around it.
- **Priority:** A (the goal), B (important, trained through), C (training race). Write why into `memory/goals.md`.

## An extra race in a full calendar

Lay out the calendar (weeks between the races and what each needs), the cost (recovery days, sessions lost before the main goal) and what the race would give (fun, experience, a long training stimulus). Recommend clearly, then let the athlete decide; a decision between distinct options suits buttons. If the decision depends on something outside training, keep the race at `maybe`, plan for the main goal until it is settled, and say what changes once it is. Record the decision when it comes (`coach races change --status`, `--add-note`).

## The last ten days or so

- **Taper:** less volume, some short intensity kept, all of it in `coach plan`. Sleep and health matter more than any session now; a missed easy run is fine.
- **Race plan,** a few days out, in one message: pacing from current fitness and the course (earlier runs on it: `coach analysis route find`; where time went last time: `route compare`; the climbs: `coach analysis activity` of that run), effort or heart rate on the climbs rather than pace, fuelling and drinking, kit, the weather at start time (`coach weather "<place>" --date <race day>`), when to get there.

## Race morning

Short: how the night was if it has synced (one line, no worrying), the weather at the start, two or three cues. Nothing new.

## After the race

- Result: `coach races result` with the official time when the athlete gives it (the watch time is stored otherwise); then the debrief as in `post-run.md`, with the heart rate checked before it is used.
- Recovery: easy days guided by the legs and the nights, not by a fixed formula; quality again when the soreness is gone.
- Memory: new bests in `fitness.md` (official time, and the watch time if different), a line in `journal.md` with what was learned.
