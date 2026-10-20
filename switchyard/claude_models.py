"""Audited Claude model effort policy: the single source for validation and execution.

Each entry states HOW generic effort is represented for an exact Claude CLI model
selector (alias or explicit ID). Lookup is exact: no prefix/family matching, no
fallback, no substitution. A selector absent from the table is unaudited.

Audit evidence: local Claude Code 2.1.299 (`--help` and its bundled model catalog),
no network discovery. See docs/design/provider-stages.md.
"""
from enum import Enum
from types import MappingProxyType


class ClaudeEffort(Enum):
    NATIVE = 'native_effort'                    # --effort <level>
    THINKING_TOKENS = 'thinking_token_budget'   # MAX_THINKING_TOKENS budget
    NONE = 'no_audited_effort'                  # usable only without effort


_N, _T, _X = ClaudeEffort.NATIVE, ClaudeEffort.THINKING_TOKENS, ClaudeEffort.NONE

CLAUDE_MODELS = MappingProxyType({
    # CLI aliases. The CLI resolves them; its first-party targets all support native effort.
    'haiku': _N, 'sonnet': _N, 'opus': _N, 'fable': _N,
    # Explicit IDs with the native effort capability.
    'claude-haiku-5-5': _N,
    'claude-sonnet-4-6': _N, 'claude-sonnet-5': _N, 'claude-sonnet-5-5': _N,
    'claude-opus-4-6': _N, 'claude-opus-4-7': _N, 'claude-opus-4-8': _N,
    'claude-opus-5': _N, 'claude-opus-5-5': _N,
    'claude-fable-5': _N, 'claude-fable-5-1': _N,
    # No native effort, but budgeted extended thinking.
    'claude-haiku-4-5': _T, 'claude-haiku-4-5-20251001': _T,
    # The CLI declares neither effort nor adaptive thinking; no translation is audited.
    'claude-sonnet-4-5': _X, 'claude-sonnet-4-5-20250929': _X,
})

THINKING_TOKENS = MappingProxyType({'low': '1024', 'medium': '8192', 'high': '16384'})
THINKING_OUTPUT_TOKENS = '32768'

# Inherited controls that could replace the explicit invocation policy: effort/thinking
# levels, alias retargeting, and model capability/catalog overrides.
CLAUDE_POLICY_ENVIRONMENT = (
    'CLAUDE_CODE_EFFORT_LEVEL', 'MAX_THINKING_TOKENS', 'CLAUDE_CODE_DISABLE_THINKING',
    'CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING', 'CLAUDE_CODE_ALWAYS_ENABLE_EFFORT',
    'CLAUDE_CODE_MODEL_CAPABILITIES', 'CLAUDE_CODE_MODEL_CATALOG', 'CLAUDE_CODE_MODEL_CATALOG_URL',
    *(f'ANTHROPIC_DEFAULT_{family}_MODEL{suffix}' for family in ('HAIKU', 'SONNET', 'OPUS', 'FABLE')
      for suffix in ('', '_SUPPORTED_CAPABILITIES')),
)


class ClaudeEffortError(ValueError):
    """The requested effort has no audited representation for this model."""


def claude_effort_controls(model, effort):
    """Return (argv, environment) representing effort for the exact resolved model.

    Raises ClaudeEffortError when effort is requested without an audited mechanism.
    """
    if effort is None:
        return (), {}
    if effort not in THINKING_TOKENS:
        raise ClaudeEffortError(f'unsupported Claude effort {effort!r}')
    mechanism = CLAUDE_MODELS.get(model)
    if mechanism is None:
        raise ClaudeEffortError(f'Claude effort translation is not audited for resolved model {model!r}; '
                                'omit effort or select an audited model')
    if mechanism is ClaudeEffort.NONE:
        raise ClaudeEffortError(f'Claude effort translation is not audited for resolved model {model!r}: '
                                'it has no effort mechanism; omit effort')
    if mechanism is ClaudeEffort.NATIVE:
        return ('--effort', effort), {}
    return (), {'MAX_THINKING_TOKENS': THINKING_TOKENS[effort],
                'CLAUDE_CODE_MAX_OUTPUT_TOKENS': THINKING_OUTPUT_TOKENS}
