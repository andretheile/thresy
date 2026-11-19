# Planning a week or a block

The athlete asks for a plan, or the current one runs out. A good plan fits the athlete's life, serves the next goal race, uses their current numbers, and is written into `coach plan`, so that it still exists tomorrow when you have forgotten this conversation.

## Inputs

- **Goals:** `coach races list` and `memory/goals.md`. How many weeks to the next A race, and what does it demand (distance, hills, surface)?
- **Life:** availability, constraints and preferences are in memory. Do not ask what memory already knows; ask only about this week's specifics, and only if they change the plan.
- **Load:** `coach analysis weeks --weeks 8`. What has the athlete actually been running, and how long were the long runs? A plan that suddenly halves or doubles that needs a stated reason.
- **Recovery:** `coach analysis wellness --days 14`, last night, any illness in the journal.
- **Already planned:** `coach plan show` and `coach plan show next`. Change what is there rather than adding a second version.
- **Weather:** `coach weather --days 7` before placing outdoor quality sessions and long runs. Rain can make a track or a trail unusable, heat moves hard sessions to the morning, wind matters on exposed courses.

## Principles

- **Targets from current numbers.** Paces and heart rates from `memory/fitness.md` and the last few weeks' sessions, written into every quality session. When the markers are older than about six weeks or the recent sessions contradict them, trust the recent sessions and update memory.
- **The athlete's own formats.** Use the session types, rep lengths and recoveries they know and like (memory), unless there is a reason to change; then give the reason.
- **Hard and easy.** As many hard sessions as the athlete is used to, shaped by the goal race; easy days easy. Long run and hard sessions spaced so each can be done well.
- **Flexibility.** Mark the key session of the week, and say what to drop first if nights are bad or time is short.

## Output

Write every session with `coach plan add`: a title in the athlete's words, `--kind`, the targets (`--distance-km`, `--duration-min`, `--pace`, `--hr`, `--ascent-m`) and a `--description` with warm-up, reps, recoveries and cues. Then answer with one line per day and a sentence on the idea of the week:

> Mon easy 10 km, HR under 150
> Tue key session: 4×8 min at 4:05–4:10, 2 min jog
> Wed rest or 30 min very easy
> …

Ask before writing the plan only when something essential is unknown (which days are possible at all).
