"""Seeding and refreshing the coach workspace from the template in the repository's
``workspace/`` directory (``coach workspace init``).

The workspace volume belongs to the agent once it is seeded. The template only keeps the files
it owns up to date: ``AGENTS.md``, ``CLAUDE.md`` and ``playbooks/``. Everything else (above
all ``memory/``, ``scripts/``, ``out/`` and ``inbox/``, and whatever the agent or the athlete
created) is copied on the first run into an empty workspace, or created empty when missing, and
never touched again. See docs/deploy.md and docs/workspace.md.

``coach workspace scripts`` (:mod:`thresy.workspace.scripts`) prints the index of the analysis
scripts the coach keeps in ``scripts/``, built from their headers.
"""

from .scripts import ScriptEntry, ScriptIndex, index_scripts
from .seed import (
    AGENT_OWNED,
    CREATED_DIRS,
    TEMPLATE_OWNED,
    InitResult,
    WorkspaceError,
    default_template,
    import_memory,
    import_scripts,
    init_workspace,
)

__all__ = [
    "AGENT_OWNED",
    "CREATED_DIRS",
    "TEMPLATE_OWNED",
    "InitResult",
    "ScriptEntry",
    "ScriptIndex",
    "WorkspaceError",
    "default_template",
    "import_memory",
    "import_scripts",
    "index_scripts",
    "init_workspace",
]
