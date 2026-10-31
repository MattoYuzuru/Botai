"""The v4 action engine: owner policy, executor, journal and tools (phase 3.1, D-006).

Write actions are allowed (D-006); the owner sets autonomy per scope and class in
`action_policies` (`auto` / `ask` / `never`), every concrete action is journaled in `actions_log`
(method, URL, `body_sha256`, redacted preview, decision, result) and is idempotent by its
idempotency key. The tools built on top of this package are `action_propose`,
`action_execute` and `action_history` (agents/v4/tools/actions.py).

Nothing here prints or stores a primary secret: secret-looking values are masked before a
proposal is written, and the executed request is stored without its body — only its hash.
"""

from botai.actions.engine import (
    ActionEngine,
    ActionReport,
    JournalEntry,
    PreparedAction,
)
from botai.actions.executor import ActionExecutor, ExecutionOutcome
from botai.actions.models import ActionClass, ActionMode, ActionRequest, BrowserStep, ScopeKind
from botai.actions.policy import (
    DEFAULT_MODES,
    ActionPolicyDecider,
    ResolvedPolicy,
    infer_action_class,
    resolve_policy,
)
from botai.db.models import ACTION_CLASSES, ACTION_MODES, POLICY_SCOPE_KINDS

__all__ = [
    "ACTION_CLASSES",
    "ACTION_MODES",
    "DEFAULT_MODES",
    "POLICY_SCOPE_KINDS",
    "ActionClass",
    "ActionEngine",
    "ActionExecutor",
    "ActionMode",
    "ActionPolicyDecider",
    "ActionReport",
    "ActionRequest",
    "BrowserStep",
    "ExecutionOutcome",
    "JournalEntry",
    "PreparedAction",
    "ResolvedPolicy",
    "ScopeKind",
    "infer_action_class",
    "resolve_policy",
]
