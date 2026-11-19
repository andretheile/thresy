# A new athlete

Memory still holds only the skeletons. Fill it over the first conversations without interrogating anyone: learn what the data can tell you first, then ask for the rest a few questions at a time.

## First, from the data (do not ask for any of this)

- Bests: `coach sql "SELECT label, value, date, activity_name, source FROM personal_records ORDER BY key"` (values in seconds or metres; Garmin's bests are watch times, so ask for official race times).
- Volume and long runs: `coach analysis weeks --weeks 26`.
- Session types, typical easy pace and heart rate, races: `coach analysis list --days 90`, and `coach analysis intervals` on a recent hard session.
- Sleep, HRV range, resting heart rate: `coach analysis wellness --days 28`.

Write what you found into `memory/fitness.md`, marked "from data" with the date.

## Then ask, in this order, two or three questions per message

1. **Goals:** the next races and what the athlete wants from them; the bigger aim of the season.
2. **Life:** which days and how long they can run, at what time; work, family and travel as they bear on training; fixed constraints (terrain, daylight, time of day).
3. **History:** injuries and recurring problems, the most they have trained in a good phase, the sessions they like.
4. **Coaching:** how much detail they want, how direct, whether to check the weather before each plan, anything you should always or never do.
5. **Kit:** watch, chest strap, anything that affects the data.

Confirm what the data suggest instead of asking open questions: "Your best 10 km in the data is 41:20 from April; is there an official time that is faster?" Use buttons when the answers are choices.

## Write as you go

After every answer, update memory (and `coach races` for races). Do not plan a full week before goals and availability are known; until then suggest a few days of easy running and say why.
