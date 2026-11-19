"""Analysis of activities, segments, intervals and wellness: library functions and the
``coach analysis`` commands.

Modules (each usable from Python without the CLI):

* :mod:`~thresy.analysis.data`: selecting activities and loading records and laps as pandas
  frames (``load_records``), plus the shared time and distance helpers.
* :mod:`~thresy.analysis.activity`: splits, halves, heart-rate drift, climbs, stops.
* :mod:`~thresy.analysis.intervals`: work and recovery segments, per-rep metrics, and the
  comparison of sessions of the same kind.
* :mod:`~thresy.analysis.segments`: fixed segments (start and end point), efforts, history.
* :mod:`~thresy.analysis.route`: two activities on the same course aligned by position.
* :mod:`~thresy.analysis.sensor`: heart-rate plausibility (cadence lock, jumps, spikes).
* :mod:`~thresy.analysis.night`: sleep stages and the HRV readings of one night.
* :mod:`~thresy.analysis.wellness`: HRV, resting heart rate and sleep over N days.
* :mod:`~thresy.analysis.weeks`: weekly volume and year-to-date totals.
* :mod:`~thresy.analysis.plots`: matplotlib figures in one style, labels in ``COACH_LANGUAGE``.

Everything only reads, except the segment commands, which write ``segments`` and the
``segment_efforts`` cache (allowed for the agent's role) and fall back to computing without the
cache on a read-only connection. Definitions of every number are in ``docs/analysis.md``.
"""
