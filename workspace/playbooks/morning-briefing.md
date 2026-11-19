# Morning briefing

The bridge starts this run once a morning, when last night's sleep has arrived, or at a fallback time without it. The athlete reads it on the phone before deciding about the day's run. A good briefing is a few lines that answer three things: how recovered am I, what is today's session, and does anything change it.

## On the way

1. **Last night.** Only the night with `wake_date` = today counts; `coach analysis night` shows it by default. If the prompt says the night has not been synced, write one line saying so and go on without it. Never fill the gap with an older night. If the prompt says the night may be incomplete or HRV is missing, say so where it matters.
2. **Recovery in context.** `coach analysis wellness --days 14`: HRV against its balanced range and the last few nights, resting heart rate against its usual level, sleep against what is normal for this athlete (memory). Garmin's status word follows the 7-day average and lags; describe what the numbers do. A single low night after waking a lot, a late evening or alcohol is weak evidence; `coach analysis night` shows whether awake periods pulled the average down (HRV while asleep, first 3 hours). Two or three low nights with a raised resting heart rate are real.
3. **Yesterday.** `coach plan show`. Mark yesterday's workouts done (`coach plan done` links the run) or skipped. A planned run with no activity: either the watch has not synced or the athlete did not run, so ask, in half a sentence. If the earlier messages in the prompt show a debrief of yesterday's run, do not repeat it.
4. **Today.** What is planned, and whether recovery, illness or the weather change it. If you change it, change it in `coach plan` too. Check the weather for the time the athlete usually runs (memory): `coach weather`.
5. **Nothing planned** (a rest day, a gap in the plan): still say how the night was and what you suggest. Reply `NO_REPLY` only if there is truly nothing useful to say.

## Shape

Four to eight lines, no headings, no greeting formula beyond a word. The verdict first (go as planned, easier, rest), then the night in one or two sentences with its numbers, then today's session with targets, then the weather if it matters. At most one question.

The two examples below are illustrative only, with invented numbers. Take the facts, the wording and even the structure from today's data and from what this athlete needs; do not copy their form.

> Good to go: 7:40 h, score 82, HRV 64 ms back inside your range (58–72) after two low nights, resting HR 47 as usual.
> Today as planned: 5×1 km at 3:50–3:55, 2 min jog. Dry and 9 °C until early afternoon, gusts from 15:00, so before lunch if you can.
> How are the calves after Sunday?

> Last night isn't synced yet. Open the Garmin app and tell me, then I'll look.
> Yesterday's 12 km easy is in the plan as done. Today is the rest day; tomorrow the long run, 24 km, and it looks dry.
