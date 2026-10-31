"""Runtime and work-schema identity (#5 CONTINUITY-004). The only place historical work formats are named.

Work schemas that actually exist:
  1  lifecycle contract 1 (pre lifecycle-v2 configuration): not adoptable.
  2  lifecycle contract 2, in-place delivery in the caller's checkout: release 2.3.0, and
     `isolation=False` work today.
  3  lifecycle contract 2 with an isolated delivery branch and worktree (`isolation` provenance).
Works before this slice carry no explicit `work_schema_version` or `runtime_version`; their schema is
identified from structural markers only when that is unambiguous. New works record both.
"""
from pathlib import Path
import re

CURRENT_WORK_SCHEMA = 3


class SchemaError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def runtime_version():
    """This runtime's package version (source tree first, then installed metadata); None if unknown."""
    pyproject = Path(__file__).resolve().parents[1]/'pyproject.toml'
    try:
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding='utf-8'))
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version('switchyard-runtime')
    except Exception:
        return None


def runtime_stamp(isolated):
    """Fields every new baseline records."""
    return dict(work_schema_version=CURRENT_WORK_SCHEMA if isolated else 2, runtime_version=runtime_version())


def work_schema(baseline):
    """The work schema of a recovery baseline. Never guesses: a missing lifecycle field is the runtime's own
    convention for contract 1; anything contradictory, malformed or newer than this runtime is refused."""
    if type(baseline) is not dict or not all(k in baseline for k in ('snapshot', 'flow', 'config')):
        raise SchemaError('ambiguous_source_schema: baseline lacks snapshot/flow/config')
    declared = baseline.get('work_schema_version')
    if declared is not None and (type(declared) is not int or declared < 1):
        raise SchemaError('ambiguous_source_schema: invalid work_schema_version %r' % (declared,))
    if declared is not None and declared > CURRENT_WORK_SCHEMA:
        raise SchemaError('source_schema_too_new: work schema %d > %d' % (declared, CURRENT_WORK_SCHEMA))
    lifecycle = baseline.get('lifecycle_contract_version', 1)
    if type(lifecycle) is not int or lifecycle < 1:
        raise SchemaError('ambiguous_source_schema: invalid lifecycle_contract_version %r' % (lifecycle,))
    if lifecycle > 2:
        raise SchemaError('source_schema_too_new: lifecycle contract %d' % lifecycle)
    structural = 1 if lifecycle == 1 else (3 if 'isolation' in baseline else 2)
    if declared is not None and declared != structural:
        raise SchemaError('ambiguous_source_schema: declared schema %d contradicts structure %d' % (declared, structural))
    return structural
