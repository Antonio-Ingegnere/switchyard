"""OBSERVABILITY (#1) slice 4: a sanitized, bounded, deterministic debug bundle of one work.

The bundle is an EXPORT of proven observability evidence, never runtime state. Every exported object is built from an
explicit allowlist: each source record is parsed, its schema checked, and every exported field type-checked against a
conservative shape (identifier, commit SHA, digest, count, timestamp, enum). A value that fails its check is not
exported; its field is listed under `excluded_for_safety`. Free text (summaries, error messages, diagnostics),
paths, URLs, configuration contents, human answers and anything not on the allowlist are never exported. Unknown
fields are dropped and counted.

Reads are pure and lock-free: no controller lock, no recovery, no backfill, no Git, no network, no provider. The
only write is the requested archive: staged as a hidden temporary next to the destination, then published with an
exclusive hard link (never overwriting), and removed on any failure.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

from .claude_models import CLAUDE_MODELS
from .diagnostics import SCHEMA_VERSION as DIAGNOSTIC_SCHEMA, diagnostic_id, file_name
from .logs import _NAMES as _STATUS_NAMES, latest_record, observed_status, read_regular
from .model_capabilities import IMAGE_INPUT_MODELS
from .provider_stages import result_field_map
from .trace import EVENT_TYPES, SCHEMA_VERSION as TRACE_SCHEMA

BUNDLE_SCHEMA_VERSION = 1
MAX_EVENTS = 10_000
MAX_DIAGNOSTICS = 5_000
MAX_RECORD = 32 * 1024            # one exported record
MAX_SOURCE_RECORD = 64 * 1024     # one source record read (writers bound theirs at 32 KiB); larger = invalid source
MAX_UNCOMPRESSED = 16 * 1024 * 1024
MAX_COMPRESSED = 16 * 1024 * 1024
MAX_ISSUES = 50                   # listed issues; the counts are always complete
MAX_LIST = 256                    # items in one exported list field
FILES = ('manifest.json', 'README.md', 'runtime.json', 'status.json', 'trace.jsonl', 'diagnostics.jsonl')
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class BundleError(ValueError):
    pass


# --- Value shapes ---------------------------------------------------------------------------------------------
_RUN = r'run:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_PATTERNS = {
    'id': re.compile(r'[A-Za-z0-9@][A-Za-z0-9_.:@+=/-]{0,127}'),
    'run': re.compile(_RUN), 'visit': re.compile(_RUN+r':visit:[0-9]{1,9}'), 'inv': re.compile(_RUN+r':invocation:[0-9]{1,9}'),
    'hex32': re.compile(r'[0-9a-f]{32}'), 'sha': re.compile(r'[0-9a-f]{64}'), 'git': re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?'),
    'ts': re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z'),
    'jpath': re.compile(r'[A-Za-z0-9_$][A-Za-z0-9_.\[\]]{0,127}'),
    'version': re.compile(r'[0-9]{1,4}(?:\.[0-9]{1,6}){1,3}(?:[.+-][A-Za-z0-9.]{1,32})?'),
}

# --- Pseudonymization --------------------------------------------------------------------------------------
# Every user-controlled name (flow, state, agent, model, artifact, outcome, role, branch, remote, executable,
# tool, work id, foreign field names in validation paths) is replaced by `<category>-<12 hex>`: an HMAC of the name
# under a bundle key derived from data that never enters the bundle (the frozen configuration text; for works
# without a baseline, the canonical history). Equal names map to equal aliases across all files; the mapping is
# never exported, does not depend on iteration order, and cannot be rebuilt by guessing names without that data.
# Kept as-is: Switchyard's own vocabulary (snake_case codes, event types, statuses, QA_READY), the built-in
# providers, the audited built-in models and tools, and generated roles (`state-<n>`).
from .recovery import QA_READY  # noqa: E402  (lifecycle constant, imported late to keep the module light)

_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}|SIG[A-Z0-9]{1,16}|StageResult')
_PROVIDERS = frozenset({'claude', 'codex', 'command'})
_MODELS = frozenset(CLAUDE_MODELS).union(*IMAGE_INPUT_MODELS.values())
_TOOLS = frozenset({'Read', 'Edit', 'Write', 'Glob', 'Grep', 'Bash'})
_STATUSES = frozenset(_STATUS_NAMES.values()) | {'UNKNOWN', QA_READY}
_METRICS = frozenset({'max_state_visits_per_run', 'max_invocations_per_run'})
_CAPABILITIES = frozenset({'repo_read', 'repo_write', 'command'})
_RESULT_FIELDS = frozenset(k.rstrip('[]').split('.')[-1] for k in result_field_map() if k != '$') | frozenset(
    name for names in result_field_map().values() for name in names)
_RUNTIME_STATE = re.compile(r'@v2:([^:]+):([^:]+):([0-9]{1,4})')
_SCOPE = re.compile(r'@v2-scope:([^:]+):([^:]+):(state|rung:[0-9]{1,4})')
ALIAS_KINDS = frozenset({'work', 'flow', 'state', 'rstate', 'scope', 'metric', 'agent', 'model', 'provider', 'artifact',
                         'outcome', 'role', 'branch', 'remote', 'executable', 'tool', 'status', 'code', 'capability',
                         'per_visit_of'})


class _Aliases:
    def __init__(self, key):
        self.key, self.seen = key, {}

    def alias(self, category, value):
        digest = hmac.new(self.key, ('%s\0%s' % (category, value)).encode('utf-8'), hashlib.sha256).hexdigest()
        alias = '%s-%s' % (category, digest[:12])
        if self.seen.setdefault(alias, value) != value:
            raise BundleError('pseudonym collision; export aborted')  # Never a silent merge of two identities.
        return alias

    def rekey(self, category, value):
        """A 32-hex identity re-derived under the bundle key (event and diagnostic ids hash the work id)."""
        return hmac.new(self.key, ('%s\0%s' % (category, value)).encode('utf-8'), hashlib.sha256).hexdigest()[:32]

    def name(self, kind, v):
        a = self.alias
        if kind == 'code':
            return v if _CODE.fullmatch(v) else a('value', v)
        if kind == 'provider':
            return v if v in _PROVIDERS else a('provider', v)
        if kind == 'model':
            return v if v in _MODELS else a('model', v)
        if kind == 'state':
            return v if v == QA_READY else a('state', v)
        if kind == 'status':
            return v if v in _STATUSES else a('state', v)  # A recorded terminal is a workflow state name.
        if kind == 'outcome':
            return v if v == 'budget_exceeded' else a('outcome', v)
        if kind == 'tool':
            return v if v in _TOOLS else a('tool', v)
        if kind == 'role':
            return v if re.fullmatch(r'state-[0-9]{1,4}', v) else a('role', v)
        if kind == 'capability':
            return v if v in _CAPABILITIES else a('value', v)
        if kind in ('rstate', 'per_visit_of'):
            m = _RUNTIME_STATE.fullmatch(v)
            if m:
                return '@v2:%s:%s:%s' % (a('flow', m.group(1)), self.name('state', m.group(2)), m.group(3))
            return self.name('state', v)
        if kind == 'scope':
            m = _SCOPE.fullmatch(v)
            return ('@v2-scope:%s:%s:%s' % (a('flow', m.group(1)), self.name('state', m.group(2)), m.group(3))
                    if m else a('scope', v))
        if kind == 'metric':
            if v in _METRICS:
                return v
            return 'invocation_scope:'+self.name('scope', v[17:]) if v.startswith('invocation_scope:') else a('value', v)
        if kind == 'branch':
            for prefix, category in (('refs/heads/switchyard/', 'work'), ('refs/heads/', 'branch')):
                if v.startswith(prefix) and len(v) > len(prefix):
                    return prefix + a(category, v[len(prefix):])
            return a('branch', v)
        return a(kind, v)  # work, flow, agent, artifact, remote, executable

    def path(self, v):
        """A validation JSON path with every segment outside the StageResult contract aliased."""
        out = []
        for part in re.split(r'(\.|\[[0-9]{1,6}\])', v):
            out.append(part if part in ('', '.', '$') or re.fullmatch(r'\[[0-9]{1,6}\]', part) or part in _RESULT_FIELDS
                       else self.alias('field', part))
        return ''.join(out)
_REF = dict(kind='code', name='artifact', version='int', sequence='int', sha256='sha', byte_size='int', omitted='int', path='text')
_BUDGET = dict(semantic_charged='bool', run_invocations=dict(used_before='int', used_after='int', limit='int'),
               invocation_scopes=[dict(scope='scope', per_visit_of='per_visit_of', used_before='int', used_after='int', limit='int')],
               state_visits=dict(used='int', limit='int'), infrastructure_failures=dict(agent='agent', used='int', limit='int'))
_ORDINALS = dict(invocation_in_visit='int', semantic_attempt='int', turn='int')
# Kinds: value shapes (`_PATTERNS`, int, num, bool); name kinds (`ALIAS_KINDS`: checked, then kept or pseudonymized);
# 'eid'/'did' re-keyed ids; 'skip' = never exported (structural or exported elsewhere); 'text' = free text or a path:
# never exported, and listed under `excluded_for_safety` when the source held a value.
_PAYLOADS = {
    'run_started': dict(provenance='text'),  # Provenance is exported once, sanitized, in runtime.json.
    'state_entered': dict(visit_number='int', max_state_visits_per_run='int', terminal='bool', wait_for='code'),
    'routing_decided': dict(requested_state='state', rung='int', role='role', agent='agent', provider='provider', model='model',
                            effort='code', workspace='code', shell='bool', capabilities=['capability'], timeout_seconds='num',
                            context_mode='code', escalation_kind='code', policy='code', policy_identity='rstate',
                            fallback_used='bool'),
    'invocation_started': dict(_ORDINALS, budget=_BUDGET, git=dict(head='git', worktree='text')),
    'invocation_finished': dict(_ORDINALS, outcome='outcome', outcome_category='code', termination='code', started_at='ts',
                                finished_at='ts', duration_ms='int', timeout_seconds='num', diagnostic_id='did',
                                artifact_refs=[_REF]),
    'budget_exceeded': dict(metric='metric', limit='int', observed='int', route_to='state'),
    'transition_decided': dict(from_state='state', from_visit='visit', to_state='state', to_runtime_state='rstate', to_rung='int',
                               trigger='code', outcome='outcome'),
    'state_exited': {},
    'human_input_requested': dict(request_id='visit', target='state'),
    'human_input_received': dict(request_id='visit', target='state'),
    'run_resumed': dict(resume_kind='code', target='state', context=dict(
        from_terminal='state', resume_to='state', qa_fix_round='int', candidate=dict(identity='sha'),
        inferred_interruption=dict(invocation_id='inv', semantic_invocation='bool'))),
    'run_interrupted': dict(_ORDINALS, started_at='ts', reason='code', outcome_category='code', semantic_invocation='bool',
                            termination='code', exit_code='int', duration_ms='int', resume_safe='bool', diagnostic_id='did',
                            diagnostic='text'),
    'run_finished': dict(terminal='state', duration_ms='int'),
}
_ENVELOPE = dict(sequence='int', event_id='eid', event_type='code', work_id='work', run_id='run', state_visit_id='visit',
                 invocation_id='inv', state='state', runtime_state='rstate', rung='int', agent='agent', provider='provider',
                 model='model', timestamp='ts', observed_by='code', schema_version='skip', source='skip', runtime='skip',
                 payload='skip')
_DIAGNOSTIC = dict(
    schema_version='skip', sources='skip', summary='text', diagnostic_id='did', work_id='work', run_id='run',
    state_visit_id='visit', invocation_id='inv', state='state', runtime_state='rstate', rung='int', agent='agent',
    provider='provider', model='model', observed_by='code', completeness='code', timeout_seconds='num', duration_ms='int',
    duration_source='code', truncated='bool',
    result=dict(produced_stage_result='bool', outcome='outcome', outcome_category='code', classification='code', reason='code',
                semantic_invocation='bool', resume_safe='bool', error='text'),
    process=dict(launched='bool', exit_code='int', termination_signal='code', terminated_by='code', timed_out='bool',
                 timeout_seconds='num', duration_ms='int', launches='int', source='code'),
    provider_channel=dict(decodable='bool', result_envelope='bool', subtype='code', terminal_reason='code', api_error_code='code',
                          api_error_status='int', is_error='bool', num_turns='int', permission_denials=['tool'],
                          errors='text', errors_omitted=dict(count='int', reason='code')),
    stderr_excerpt=dict(stream='code', available='bool', reason='code', bytes_seen='int', retained_bytes='int', truncated='bool',
                        text='text'),
    structured_output=dict(schema=dict(name='code', transport='code', sha256='sha'), valid='bool', errors_truncated='bool',
                           raw_result_retained='bool', error='text', errors='skip',
                           repair=dict(mechanism='code', max_attempts='int', attempted='bool', succeeded='bool')),
    verification=dict(verifier='agent', executable='executable', argc='int', argv_sha256='sha', exit_code='int', timed_out='bool',
                      outcome='outcome', outcome_rule='code', evidence_artifact='artifact', evidence_refs=[_REF]),
    artifact_refs=[_REF])
_ERROR_CATEGORIES = (('additional property is not allowed', 'additional_property'),
                     ('required property is missing', 'missing_required'), ('expected ', 'type_mismatch'))


def _scalar(value, kind):
    if kind == 'int':
        return type(value) is int and -2**53 < value < 2**53
    if kind == 'num':
        return type(value) in (int, float) and value == value and abs(value) < 2**53
    if kind == 'bool':
        return type(value) is bool
    return (type(value) is str and _PATTERNS[kind].fullmatch(value) is not None
            and ('..' not in value if kind == 'id' else True))


_OMIT = object()


class _Shaper:
    """Builds allowlisted objects; records excluded (code-owned paths) and unrecognized (counted, never named) fields."""

    def __init__(self, aliases):
        self.excluded, self.unrecognized, self.aliases = [], 0, aliases

    def shape(self, value, spec, path):
        if spec == 'skip':
            return _OMIT
        if spec == 'text':
            if value not in (None, '', [], {}):
                self.excluded.append(path)
            return _OMIT
        if value is None:
            return None
        if isinstance(spec, dict):
            if type(value) is not dict:
                self.excluded.append(path)
                return None
            self.unrecognized += len([k for k in value if k not in spec])
            out = {}
            for key, sub in spec.items():
                shaped = self.shape(value.get(key), sub, _join(path, key))
                if shaped is not _OMIT:
                    out[key] = shaped
            return out
        if isinstance(spec, list):
            if type(value) is not list or len(value) > MAX_LIST:
                self.excluded.append(path)  # Never silently truncated.
                return None
            return [self.shape(item, spec[0], path+'[]') for item in value]
        if spec in ALIAS_KINDS:
            if _scalar(value, 'id'):
                return self.aliases.name(spec, value)
            self.excluded.append(path)
            return None
        if spec in ('eid', 'did'):
            if _scalar(value, 'hex32'):
                return self.aliases.rekey(spec, value)
            self.excluded.append(path)
            return None
        if _scalar(value, spec):
            return value
        self.excluded.append(path)
        return None


def _join(path, key):
    return key if not path else path+'.'+key


def _canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)+'\n').encode('ascii')


# --- Source scanning ------------------------------------------------------------------------------------------
_EVENT_NAME = re.compile(r'([0-9]{6})-([0-9a-f]{32})\.json')


class _Report:
    def __init__(self):
        self.issues, self.counts = [], {}

    def add(self, area, reason, **where):
        self.counts[reason] = self.counts.get(reason, 0)+1
        if len(self.issues) < MAX_ISSUES:
            self.issues.append(dict(area=area, reason=reason, **where))


def _directory(root, *parts):
    """A store directory under the work, or a reason it cannot be read safely."""
    current = Path(root)
    for part in parts:
        current = current/part
        if current.is_symlink():
            return None, 'excluded_for_safety'
        if not current.is_dir():
            return None, 'unavailable'
    return current, None


def _record(directory, name, report, area, where):
    data = read_regular(directory/name, MAX_SOURCE_RECORD)
    if data is None:
        report.add(area, 'unreadable_symlinked_or_oversized_record', **where)
        return None
    try:
        record = json.loads(data.decode('utf-8'))
    except (UnicodeError, ValueError, RecursionError):
        report.add(area, 'corrupt_record', **where)
        return None
    if type(record) is not dict:
        report.add(area, 'corrupt_record', **where)
        return None
    return record


def _trace(root, report, sink, aliases):
    """Stream sanitized trace events into `sink`; returns facts for correlation and completeness."""
    directory, reason = _directory(root, 'observability', 'events')
    facts = dict(available=directory is not None, reason=reason, events=0, sequences=[], invocations={}, sources=set(),
                 unsupported=0, corrupt=0, excluded=0, unrecognized=0, ids=set())
    if directory is None:
        return facts
    names = []
    for name in sorted(os.listdir(directory)):
        match = _EVENT_NAME.fullmatch(name)
        if match is None:
            if not name.startswith('.tmp-'):  # In-flight publication temporaries are not evidence.
                report.add('trace', 'unexpected_entry')
            continue
        names.append((int(match.group(1)), match.group(2), name))
    if len(names) > MAX_EVENTS:
        raise BundleError('bundle limit exceeded: %d trace events > %d' % (len(names), MAX_EVENTS))
    for seq, event_id, name in names:
        where = dict(sequence=seq)
        record = _record(directory, name, report, 'trace', where)
        if record is None:
            facts['corrupt'] += 1
            continue
        if record.get('schema_version') != TRACE_SCHEMA:
            facts['unsupported'] += 1
            report.add('trace', 'unsupported_schema_version', **where)
            continue
        if record.get('sequence') != seq or record.get('event_id') != event_id or record.get('event_type') not in EVENT_TYPES:
            facts['corrupt'] += 1
            report.add('trace', 'record_does_not_match_its_name_or_type', **where)
            continue
        if event_id in facts['ids']:
            facts['corrupt'] += 1
            report.add('trace', 'duplicate_event_id', **where)
            continue
        facts['ids'].add(event_id)
        shaper = _Shaper(aliases)
        out = shaper.shape(record, _ENVELOPE, '')
        source = record.get('source')
        index = source.get('index') if type(source) is dict else None
        out['source_index'] = index if _scalar(index, 'int') else None
        out['payload'] = shaper.shape(record.get('payload') or {}, _PAYLOADS[record['event_type']], 'payload')
        out['excluded_for_safety'] = sorted(set(shaper.excluded))
        facts['excluded'] += len(out['excluded_for_safety'])
        facts['unrecognized'] += shaper.unrecognized
        sink(out)
        facts['events'] += 1
        facts['sequences'].append(seq)
        if out['source_index'] is not None:
            facts['sources'].add(out['source_index'])
        if out['invocation_id'] is not None:
            state = facts['invocations'].setdefault(out['invocation_id'], dict(started=False, ended=False, sequence=seq))
            state['started'] |= record['event_type'] == 'invocation_started'
            state['ended'] |= record['event_type'] in ('invocation_finished', 'run_interrupted')
    return facts


def _error_category(text):
    return next((category for prefix, category in _ERROR_CATEGORIES if type(text) is str and text.startswith(prefix)), 'other')


def _diagnostics(root, work_id, report, sink, aliases):
    directory, reason = _directory(root, 'observability', 'diagnostics')
    facts = dict(available=directory is not None, reason=reason, records=0, invocations=set(), unsupported=0, corrupt=0,
                 excluded=0, unrecognized=0)
    if directory is None:
        return facts
    names = sorted(n for n in os.listdir(directory) if n != 'schema.json' and not n.startswith('.tmp-'))
    if len(names) > MAX_DIAGNOSTICS:
        raise BundleError('bundle limit exceeded: %d diagnostic records > %d' % (len(names), MAX_DIAGNOSTICS))
    for name in names:
        record = _record(directory, name, report, 'diagnostics', {}) if name.endswith('.json') else None
        if record is None:
            if not name.endswith('.json'):
                report.add('diagnostics', 'unexpected_entry')
            facts['corrupt'] += 1
            continue
        invocation = record.get('invocation_id')
        where = dict(invocation_id=invocation) if _scalar(invocation, 'inv') else {}
        if record.get('schema_version') != DIAGNOSTIC_SCHEMA:
            facts['unsupported'] += 1
            report.add('diagnostics', 'unsupported_schema_version', **where)
            continue
        if not where or name != file_name(invocation) or (work_id and record.get('diagnostic_id') != diagnostic_id(work_id, invocation)):
            facts['corrupt'] += 1
            report.add('diagnostics', 'record_does_not_match_its_identity', **where)
            continue
        shaper = _Shaper(aliases)
        out = shaper.shape(record, _DIAGNOSTIC, '')
        structured = record.get('structured_output') if type(record.get('structured_output')) is dict else {}
        errors = structured.get('errors') if type(structured.get('errors')) is list else []
        if out.get('structured_output') is not None:
            out['structured_output']['errors'] = [
                dict(path=aliases.path(e['path']) if _scalar(e.get('path'), 'jpath') else None, category=_error_category(e.get('error')))
                for e in errors[:MAX_ISSUES] if type(e) is dict]
        out['excluded_for_safety'] = sorted(set(shaper.excluded))
        facts['excluded'] += len(out['excluded_for_safety'])
        facts['unrecognized'] += shaper.unrecognized
        sink(out)
        facts['records'] += 1
        facts['invocations'].add(invocation)
    return facts


def _canonical_count(root, report):
    """Number of canonical events (events.jsonl), to check the trace covers them; None when unverifiable."""
    path = Path(root)/'events.jsonl'
    if path.is_symlink() or not path.is_file():
        return None
    from .observability import ObservabilityError, read_events
    try:
        return sum(1 for _ in read_events(path))
    except (ObservabilityError, OSError, ValueError):
        report.add('canonical', 'canonical_history_unreadable')
        return None


# --- Provenance and status ------------------------------------------------------------------------------------
def _pick(shaper, record, spec, prefix):
    return shaper.shape({k: record.get(k) for k in spec} if type(record) is dict else None, spec, prefix)


def _runtime(root, shaper):
    from .versioning import runtime_version
    record, baseline = latest_record(Path(root)/'recovery', 'baseline')
    out = dict(exporter=dict(switchyard_version=runtime_version(), bundle_schema_version=BUNDLE_SCHEMA_VERSION),
               baseline=None, git=None, integration=None, publication=None, pull_request=None)
    if baseline is None:
        out['baseline'] = 'unavailable'
        return out, None
    iso = baseline.get('isolation') if type(baseline.get('isolation')) is dict else {}
    identity = ((baseline.get('snapshot') or {}).get('identity') or {}) if type(baseline.get('snapshot')) is dict else {}
    config = baseline.get('config')
    out['baseline'] = shaper.shape(dict(
        runtime_version=baseline.get('runtime_version'), work_schema_version=baseline.get('work_schema_version'),
        lifecycle_contract_version=baseline.get('lifecycle_contract_version'), flow=baseline.get('flow'),
        config_sha256=hashlib.sha256(config.encode('utf-8')).hexdigest() if type(config) is str else None,
        baseline_digest=record.get('sha256')),
        dict(runtime_version='version', work_schema_version='int', lifecycle_contract_version='int', flow='flow',
             config_sha256='sha', baseline_digest='sha'), 'baseline')
    branch = iso.get('delivery_branch')
    out['git'] = shaper.shape(dict(base_sha=iso.get('base_sha') or identity.get('head'), delivery_branch=branch,
                                   source_branch=iso.get('source_branch')),
                              dict(base_sha='git', delivery_branch='branch', source_branch='branch'), 'git')
    for key, store, name, spec in (
            ('integration', 'integration', 'integration_record',
             dict(phase='code', strategy='code', integrated_by='code', accepted_commit_sha='git', target_ref='branch', target_before_sha='git',
                  target_after_sha='git', integrated_commit_sha='git')),
            ('publication', 'publication', 'push_record',
             dict(phase='code', pushed_by='code', remote='remote', remote_ref='branch', remote_before_sha='git', remote_after_sha='git',
                  local_integrated_commit_sha='git')),
            ('pull_request', 'pull_request', 'pull_request_record', dict(phase='code', created_by='code'))):
        _, value = latest_record(Path(root)/store, name)
        out[key] = 'not_applicable' if value is None else _pick(shaper, value, spec, key)
    work_id = branch[len('refs/heads/switchyard/'):] if type(branch) is str and branch.startswith('refs/heads/switchyard/') else (
        'baseline-'+record['sha256'][:16] if type(record.get('sha256')) is str else None)
    return out, work_id


def _status(root, shaper):
    status = observed_status(root)  # Pure reader: no lock, no reconciliation.
    _, op = latest_record(Path(root)/'recovery', 'operation')
    op = op if type(op) is dict else {}
    return shaper.shape(dict(status=status.get('status'), phase=status.get('phase'), active=status.get('active'),
                             state=op.get('state'), reason=op.get('reason'), resume_safe=op.get('resume_safe'),
                             terminal=op.get('terminal'), request_id=op.get('request_id'),
                             accepted_commit_sha=op.get('accepted_commit_sha')),
                        dict(status='status', phase='code', active='bool', state='state', reason='code', resume_safe='bool',
                             terminal='state', request_id='visit', accepted_commit_sha='git'), 'status')


# --- Assembly -------------------------------------------------------------------------------------------------
README = '''# Switchyard debug bundle

A sanitized export of one work's observability evidence. It is not runtime state and cannot be used to resume work.

| File | Content |
|---|---|
| manifest.json | Bundle schema, exporter version, counts, completeness report, issues, SHA-256 of the other files |
| runtime.json | Runtime, workflow, configuration and Git fingerprints (no paths, URLs or configuration contents) |
| status.json | Lifecycle status read from the latest durable operation record |
| trace.jsonl | Runtime trace events in sequence order (allowlisted fields) |
| diagnostics.jsonl | Per-invocation diagnostics (allowlisted fields; no free text) |

Markers: `null` means the source does not hold the value. A field named in a record's `excluded_for_safety` was
present but not exported (free text, or a value that did not pass its type check). `not_applicable` and
`unavailable` describe whole sources. Completeness values: `complete`, `partial`, `unavailable`, `not_applicable`.

Never exported: prompts, provider responses or result envelopes, stdout/stderr, source or generated code,
environment, credentials, configuration contents, remote URLs, absolute paths, verification output, human answers,
QA evidence contents, free-text diagnostics. Sensitive data is not intentionally included.

Pseudonyms: user-chosen names (work, flow, state, agent, model, artifact, outcome, role, branch, remote, executable,
tool, foreign result fields) appear as `<category>-<12 hex>`. Equal names have equal pseudonyms throughout this bundle.
The mapping is not included and cannot be recomputed from this bundle. Switchyard's own vocabulary, the built-in
providers, audited built-in models and tools, and `READY_FOR_MANUAL_QA` are kept. Event and diagnostic ids are
re-derived for this bundle; they correlate within it, not with the source work's files.
'''


def _alias_key(root):
    """The pseudonym key: from source data never exported (the frozen configuration text, else the canonical
    history). Deterministic for an unchanged snapshot. With neither available, a random key: aliases then stay
    consistent within the bundle but differ between exports (determinism yields to confidentiality)."""
    _, baseline = latest_record(Path(root)/'recovery', 'baseline')
    material = baseline.get('config') if type(baseline) is dict and type(baseline.get('config')) is str else None
    if material is not None:
        material = b'config\0' + material.encode('utf-8')
    elif not (Path(root)/'events.jsonl').is_symlink():
        events = read_regular(Path(root)/'events.jsonl', 256 << 20)
        material = b'events\0' + events if events else None
    if not material:
        return os.urandom(32)
    return hashlib.sha256(b'switchyard-bundle-pseudonym-v1\0' + material).digest()


def build(work_root):
    """(members: name -> bytes for the small files, staged trace/diagnostics files, manifest dict). Read-only."""
    root = Path(work_root).resolve()
    if not (root/'recovery').is_dir() and not (root/'observability').is_dir() and not (root/'events.jsonl').is_file():
        raise BundleError('not a Switchyard work: %s' % root.name)
    aliases = _Aliases(_alias_key(root))
    report, shaper = _Report(), _Shaper(aliases)
    runtime, work_id = _runtime(root, shaper)
    status = _status(root, shaper)
    staged = {}
    sizes = {'trace.jsonl': 0, 'diagnostics.jsonl': 0}
    tmp = tempfile.TemporaryDirectory(prefix='switchyard-bundle-')
    for name in sizes:
        staged[name] = open(Path(tmp.name)/name, 'w+b')

    def sink(name):
        def write(record):
            data = _canonical(record)
            if len(data) > MAX_RECORD:
                raise BundleError('bundle limit exceeded: an exported %s record exceeds %d bytes' % (name, MAX_RECORD))
            sizes[name] += len(data)
            if sum(sizes.values()) > MAX_UNCOMPRESSED:
                raise BundleError('bundle limit exceeded: uncompressed size over %d bytes' % MAX_UNCOMPRESSED)
            staged[name].write(data)
        return write

    try:
        trace = _trace(root, report, sink('trace.jsonl'), aliases)
        diagnostics = _diagnostics(root, work_id, report, sink('diagnostics.jsonl'), aliases)
        canonical = _canonical_count(root, report)
    except BaseException:
        for stream in staged.values():
            stream.close()
        tmp.cleanup()
        raise
    seqs = trace['sequences']
    gaps = [] if not seqs else sorted(set(range(1, max(seqs)+1)) - set(seqs))
    for seq in gaps[:MAX_ISSUES]:
        report.add('trace', 'missing_sequence', sequence=seq)
    if canonical is not None and trace['available']:
        for index in sorted(set(range(canonical)) - trace['sources'])[:MAX_ISSUES]:
            report.add('trace', 'canonical_event_not_traced', canonical_index=index)
    untraced = 0 if canonical is None else len(set(range(canonical)) - trace['sources'])
    if not trace['available']:
        trace_completeness = 'unavailable'
    elif canonical is not None and not (trace['corrupt'] or trace['unsupported'] or gaps or untraced):
        trace_completeness = 'complete'
    else:
        trace_completeness = 'partial'  # Corrupt, unsupported, gapped, or not provable against events.jsonl.
    missing, unfinished = [], []
    for invocation, state in sorted(trace['invocations'].items(), key=lambda item: item[1]['sequence']):
        if invocation in diagnostics['invocations']:
            continue
        (missing if state['ended'] else unfinished).append(invocation)
    for invocation in missing[:MAX_ISSUES]:
        report.add('diagnostics', 'missing_diagnostic_for_finished_invocation', invocation_id=invocation)
    for invocation in unfinished[:MAX_ISSUES]:
        report.add('diagnostics', 'invocation_without_proven_end', invocation_id=invocation)
    orphans = sorted(diagnostics['invocations'] - set(trace['invocations']))
    for invocation in orphans[:MAX_ISSUES]:
        report.add('diagnostics', 'diagnostic_without_traced_invocation', invocation_id=invocation)
    if not trace['invocations'] and not diagnostics['records']:
        diagnostics_completeness = 'not_applicable' if trace_completeness == 'complete' else 'unavailable'
    elif not diagnostics['available'] and not trace['invocations']:
        diagnostics_completeness = 'unavailable'
    elif (missing or unfinished or diagnostics['corrupt'] or diagnostics['unsupported'] or orphans
          or trace_completeness != 'complete'):
        diagnostics_completeness = 'partial'
    else:
        diagnostics_completeness = 'complete'
    for stream in staged.values():
        stream.flush()
    members = {'README.md': README.encode('utf-8'), 'runtime.json': _canonical(runtime), 'status.json': _canonical(status)}
    if sum(len(v) for v in members.values()) + sum(sizes.values()) > MAX_UNCOMPRESSED:
        raise BundleError('bundle limit exceeded: uncompressed size over %d bytes' % MAX_UNCOMPRESSED)
    files = []
    for name in FILES[1:]:
        if name in members:
            digest, size = hashlib.sha256(members[name]).hexdigest(), len(members[name])
        else:
            staged[name].seek(0)
            digest, size = _digest(staged[name]), sizes[name]
        files.append(dict(path=name, sha256=digest, size_bytes=size))
    manifest = dict(
        bundle_schema_version=BUNDLE_SCHEMA_VERSION, switchyard_version=runtime['exporter']['switchyard_version'],
        work_id=aliases.name('work', work_id) if _scalar(work_id, 'id') else None,
        completeness=dict(trace=trace_completeness, diagnostics=diagnostics_completeness,
                          history='complete' if trace_completeness == 'complete' and not unfinished and status
                          and not status.get('active') else 'partial'),
        counts=dict(events=trace['events'], diagnostics=diagnostics['records'], canonical_events=canonical,
                    invocations=len(trace['invocations']), missing_diagnostics=len(missing),
                    invocations_without_proven_end=len(unfinished), corrupt_records=trace['corrupt']+diagnostics['corrupt'],
                    unsupported_schema_records=trace['unsupported']+diagnostics['unsupported'], trace_gaps=len(gaps),
                    excluded_for_safety_fields=trace['excluded']+diagnostics['excluded']+len(shaper.excluded),
                    unrecognized_fields_dropped=trace['unrecognized']+diagnostics['unrecognized']),
        sources=dict(trace=trace['reason'] or 'present', diagnostics=diagnostics['reason'] or 'present',
                     canonical_history='present' if canonical is not None else 'unavailable'),
        issue_counts=dict(sorted(report.counts.items())), issues=report.issues,
        limits=dict(max_events=MAX_EVENTS, max_diagnostics=MAX_DIAGNOSTICS, max_record_bytes=MAX_RECORD,
                    max_uncompressed_bytes=MAX_UNCOMPRESSED, max_compressed_bytes=MAX_COMPRESSED),
        files=files)
    return members, staged, tmp, manifest


def _digest(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 16), b''):
        h.update(chunk)
    return h.hexdigest()


def _entry(name):
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16  # Regular file, never a symlink entry.
    return info


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def create_bundle(work_root, output):
    """Write the debug bundle to `output` (which must not exist and must lie outside the work). Returns a summary."""
    root = Path(work_root).resolve()
    target = Path(output).absolute()
    parent = target.parent.resolve()
    destination = parent/target.name
    if _inside(destination, root):
        raise BundleError('refusing an output path inside the work directory')
    if os.path.lexists(destination):
        raise BundleError('output already exists (never overwritten): %s' % target.name)
    if not parent.is_dir():
        raise BundleError('output directory does not exist')
    members, staged, tmp, manifest = build(root)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix='.%s.' % target.name[:64], suffix='.partial', dir=parent)
        temporary = Path(name)
        with os.fdopen(fd, 'w+b') as raw:
            with zipfile.ZipFile(raw, 'w') as archive:
                archive.writestr(_entry('manifest.json'), _canonical(manifest))
                for member in FILES[1:]:
                    if member in members:
                        archive.writestr(_entry(member), members[member])
                    else:
                        staged[member].seek(0)
                        with archive.open(_entry(member), 'w') as out:
                            for chunk in iter(lambda: staged[member].read(1 << 16), b''):
                                out.write(chunk)
            raw.flush()
            if raw.tell() > MAX_COMPRESSED:
                raise BundleError('bundle limit exceeded: compressed size over %d bytes' % MAX_COMPRESSED)
            os.fsync(raw.fileno())
        try:
            os.link(temporary, destination)  # Exclusive: a destination created meanwhile is never clobbered.
        except FileExistsError:
            raise BundleError('output already exists (never overwritten): %s' % target.name) from None
        sha = hashlib.sha256(destination.read_bytes()).hexdigest()
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        for stream in staged.values():
            stream.close()
        tmp.cleanup()
    return dict(path=str(target), sha256=sha, size_bytes=destination.stat().st_size, counts=manifest['counts'],
                completeness=manifest['completeness'], files=manifest['files'], issue_counts=manifest['issue_counts'])
