"""Seeding and refreshing the coach workspace from the template in the repository's
``workspace/`` directory (``coach workspace init``).

The workspace volume belongs to the agent once it is seeded. The template only keeps the files
it owns up to date: ``AGENTS.md``, ``CLAUDE.md`` and ``playbooks/``. Everything else (above
all ``memory/``, ``out/`` and ``inbox/``, and whatever the agent or the athlete created) is
copied on the first run into an empty workspace and never touched again. See docs/deploy.md.
"""

from .seed import (
    AGENT_OWNED,
    TEMPLATE_OWNED,
    InitResult,
    WorkspaceError,
    default_template,
    import_memory,
    init_workspace,
)

__all__ = [
    "AGENT_OWNED",
    "TEMPLATE_OWNED",
    "InitResult",
    "WorkspaceError",
    "default_template",
    "import_memory",
    "init_workspace",
]
