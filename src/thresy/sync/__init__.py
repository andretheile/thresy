"""Garmin sync: passes, the poller, history import, sync requests.

Public API used by other packages:

* :func:`thresy.sync.passes.run_pass` / :func:`thresy.sync.passes.sync_once`: one pass;
* :func:`thresy.sync.agent_requests.create_request` and
  :func:`~thresy.sync.agent_requests.wait_for_request`: ask the poller for a pass (the bridge's
  ``/sync``, the agent's ``coach sync request``);
* :func:`thresy.sync.status.status_report`: "when did we last sync?".

No LLM is involved anywhere here. See ``docs/sync.md``.
"""
