# Answering a message

The athlete wrote. A good answer does what was asked, with numbers from commands you ran in this run, reads well on a phone, and leaves the plan and memory up to date.

## On the way

1. **What is being asked or decided?** A question about the data, a change to the plan, a report ("I'm ill", "bad night", "the watch has synced"), or just talk. Several messages at once: answer them together, the most important first.
2. **Get the facts before answering.** Typical asks:

| The athlete … | Look at |
|---|---|
| asks how a run went | `coach analysis activity`; `intervals` for reps; `sensor` before any heart-rate claim; earlier sessions of the same kind or course when a comparison helps (`intervals-compare`, `route find` and `route compare`, `segments history`) |
| asks what to do today or tomorrow | `coach plan show`, last night (`coach analysis night`), `coach analysis wellness --days 14`, `coach weather` |
| wants a week or a block planned | `playbooks/week-plan.md` |
| mentions a race | `coach races list`, `playbooks/race-prep.md` |
| says the watch has synced now | `coach sync request --wait 120`, then look |
| asks about history, bests, volume | `coach analysis weeks`, `coach sql` on `personal_records` and `runs` |
| sends a photo or a file | open it under `inbox/` |

3. **Comparisons:** say which sessions you left out and why; look up the temperature of past days with `coach weather --date` instead of calling it unknown; if you make a plot, send it, and then the text can be shorter.
4. **Data not there?** If the run or the night is not in the database, say so, and that opening the Garmin app and telling you is enough for you to look again.
5. **Decide.** Give one recommendation, not a menu. Offer options only when the choice depends on something only the athlete knows (how the legs feel, how the day turns out); then two to four buttons with `coach send choice` work well.
6. **The athlete pushes back** ("that's too easy", "you underestimate what I handle"): check the claim against the data in this run. If it holds, say so, change the target, and update memory. If it does not, show the numbers that say otherwise.

## Before you finish

- Did the athlete agree to, move, drop or add a session? Change `coach plan` now.
- Did you learn something lasting (a constraint, a preference, a corrected best, an illness, a race, an instruction)? Write it to memory now; a correction replaces the wrong entry.
- Every number in the answer came from a command in this run.
- Nothing is promised for later. What you said you would do is done, or in the plan.

## Shape

Answer first.

- **About data:** a sentence or two with the verdict, then the numbers that carry it (a small table for reps), then what it means for the next days.
- **Advice:** what to do, how (pace or heart rate, duration, where), and the one reason that matters.
- **General questions** (nutrition, kit, technique, research): short, and honest about how strong the evidence is.
