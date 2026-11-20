# Debrief after a run

The bridge starts this run a few minutes after a run is uploaded (uploads close together become one run). The athlete has just finished. A good debrief says how the session went against its purpose, with the two to four numbers that matter, and what follows for the next days.

## On the way

1. **What was it meant to be?** `coach plan show` for the planned workout, memory for context. Mark it done with `coach plan done ID` (it links the day's run; with two runs that day pass `--activity`). An unplanned run: work out what it was from the data.
2. **What happened.** `coach analysis activity ID`: splits, halves, heart-rate drift, climbs, stops. Reps: `coach analysis intervals ID` (pace, heart rate at the end of the reps, the drop in the first minute of recovery). A race: also `playbooks/race-prep.md` and `coach races result`.
3. **Heart rate.** `coach analysis sensor ID` before you use it. If it is suspect or unreliable, say so in one sentence and work with pace, power or the clean part. An implausible maximum on an easy run is a sensor artefact, not a finding.
4. **Compare when it adds something**: the same kind of session (`intervals-compare`), the same course (`route find`, then `route compare`), a fixed climb (`segments history`). Compare like with like and name what differs: temperature (`coach weather --date` gives past days too), terrain, recovery length, number of reps, sensor, fatigue. One comparison is a data point, not a verdict on form.
5. **What follows.** Does the rest of the week still fit? Much harder than intended, or something hurts: adjust the plan and say so.
6. **Anything lasting** (a new best, a marker such as threshold confirmed, a sensor that keeps failing): memory.

## Shape

- **Easy run:** two to four lines. Distance, pace, heart rate if valid, and one remark only if there is something to remark.
- **Quality session:** a one-line verdict against the purpose, a small table of the reps (no more than four short columns, for example rep, pace, HR at the end, drop after 60 s), two or three sentences of interpretation, the consequence for the next days.
- **Long run or race:** the verdict, the key numbers, where time was won or lost, how to recover.

Ask how it felt when the athlete has not said and the answer would change your reading.

Illustrative only, with invented numbers; the session decides what goes in:

> Threshold done as intended: five reps within 3 s of each other.
>
> | # | Pace | HR end | Drop 60 s |
> |---|---|---|---|
> | 1 | 3:52 | 164 | 27 |
> | 3 | 3:51 | 166 | 28 |
> | 5 | 3:49 | 168 | 27 |
>
> Heart rate at the end rose 4 bpm from the first to the last rep with the same recovery: controlled, not at the limit. Thursday stays easy. How did the last rep feel?
