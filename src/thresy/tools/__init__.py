"""Command-line tools the agent runs, merged into the top level of ``coach``.

* ``coach send …`` (outbox) and ``coach sql`` (read-only): :mod:`thresy.tools.cli`.
* ``coach weather``, ``coach plan …``, ``coach races …``: :mod:`thresy.tools.planning_cli`,
  with the logic in :mod:`~thresy.tools.weather`, :mod:`~thresy.tools.plan` and
  :mod:`~thresy.tools.races`.

Calendar and route tools join this package later.
"""
