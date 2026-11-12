"""OBSERVABILITY (#1) slice 1: a durable, read-only runtime trace of one work.

The trace is an index and explanation layer, never authority. The authoritative history stays where it was:
`<work>/events.jsonl` (canonical runtime events), the recovery store (baseline, operations), artifact manifest,
QA/acceptance/integration stores. Every trace event is a projection of exactly one canonical event, enriched with
facts the runtime already holds (compiled plan, frozen baseline, operation records); nothing is re-verified, no Git
command runs, no remote or provider is contacted.

Correlation (the runtime's own identities, reused, never re-minted):
- work_id         durable work identity (delivery branch suffix; non-isolated works: baseline record digest).
- run_id          `run:<uuid>`; one run spans its interruptions, resumes, human waits and QA reopens.
- state_visit_id  `<run>:visit:<n>`: one admitted entry into a state. Re-entering an interrupted visit keeps it;
                  a genuinely new visit (revisit, reopen, human continuation) gets the next n.
- invocation_id   `<run>:invocation:<n>`: one executor launch (one provider process). A retry after a provider
                  failure is a new invocation in the same visit. Ids are never reused, including voided ones.
- invocation_in_visit  ordinal of a launch within its visit (2 = a retry after an interrupted launch).
- semantic_attempt     ordinal of the CHARGED invocations within the visit (docs/vocabulary.md: an attempt is a
                       semantic retry): a proven infrastructure failure is a launch, not an attempt.
- turn            provider-internal structured-output repair turns happen inside ONE invocation and are not
                  durably counted by the runtime: recorded as unknown (null), never invented.
- rung / agent    the compiled execution binding of the runtime state (logical state, rung index, profile).

Ordering and identity: `sequence` is work-local, dense and deterministic: the projection of canonical event k is
fixed by the canonical history up to k, so a live write and a later backfill give the same sequence and the same
`event_id` (SHA-256 of work, run, canonical position, canonical kind, trace type; no timestamps). Each event is
one immutable file `observability/events/<sequence>-<event_id>.json`, published with an exclusive link: an
existing event is recognized, never duplicated or rewritten.

Failure isolation: every entry point swallows its own errors. A trace failure never reroutes, retries, charges
budget, changes lifecycle state or the delivery result; the event is simply missing (and may be backfilled later
from durable evidence when the work is next driven: `observed_by: recovery_backfill`).
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

SCHEMA_VERSION = 1
EVENT_TYPES = ('run_started', 'state_entered', 'routing_decided', 'invocation_started', 'invocation_finished',
               'budget_exceeded', 'transition_decided', 'state_exited', 'human_input_requested', 'human_input_received',
               'run_interrupted', 'run_resumed', 'run_finished')
_KIND = {'run_started': 'run_started', 'state_entered': 'state_entered', 'invocation_started': 'invocation_started',
         'invocation_finished': 'invocation_finished', 'budget_exceeded': 'budget_exceeded',
         'transition_selected': 'transition_decided', 'state_exited': 'state_exited',
         'human_input_requested': 'human_input_requested', 'human_input_received': 'human_input_received',
         'human_input_resumed': 'run_resumed', 'run_resumed': 'run_resumed', 'run_finished': 'run_finished',
         'run_error': 'run_interrupted'}
MAX_REFS = 20
MAX_TEXT = 256
_USERINFO = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]*@')
_SECRET = re.compile(r'(?i)\b(gh[pousr]_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{8,}|bearer\s+\S+|(?:token|password|secret|api[_-]?key)\s*[=:]\s*\S+)')


def _text(value, limit=MAX_TEXT):
    """Bounded, credential-stripped diagnostic text (or None)."""
    if value is None:
        return None
    text = _SECRET.sub('[REDACTED]', _USERINFO.sub(r'\1', str(value)))
    return text if len(text) <= limit else text[:limit-1]+'…'


def _iso(ns):
    return None if ns is None else datetime.fromtimestamp(ns/1e9, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _ms(ns):
    return None if ns is None else ns//1_000_000


def _store_dir(root):
    return Path(root)/'observability'


# --- Projection: canonical history -> trace events ----------------------------------------------------
class _Projector:
    """Deterministic, incremental projection. Holds only counters derived from the canonical prefix, exactly
    like the runtime's own reconstruction on resume (visits admitted, charged invocations, scope usage)."""

    def __init__(self, compiled, project, void):
        self.compiled, self.project, self.void = compiled, project, frozenset(void or ())
        self.position = 0          # canonical events consumed
        self.sequence = 0          # trace events produced
        self.invocations = 0       # charged invocations (run budget)
        self.admitted = {}         # runtime state -> admitted visits
        self.scopes = {}           # scope name -> usage
        self.visits = {}           # visit id -> dict(state, number, attempts)
        self.started = {}          # invocation id -> dict(at, attempt, visit)
        self.infra = {}            # agent -> voided invocations so far
        self.all_scopes = {s.name: s for st in compiled.flow.states.values() for s in st.invocation_budgets}

    def _plan(self, state):
        plan = self.compiled.plan.get(state) if state is not None else None
        binding = plan.binding if plan is not None else None
        return plan, binding

    def where(self, state):
        plan, binding = self._plan(state)
        return dict(state=plan.logical_state if plan is not None else state, runtime_state=state,
                    rung=binding.rung_index if binding is not None else None,
                    agent=binding.profile_name if binding is not None else None,
                    provider=str(getattr(binding.provider, 'value', binding.provider)) if binding is not None else None,
                    model=binding.model if binding is not None else None)

    def routing(self, state):
        plan, binding = self._plan(state)
        agent = self.project.agents.get(binding.profile_name) if binding is not None else None
        profile = self.compiled.profiles.get(binding.profile_name) if binding is not None else None
        effort = getattr(binding, 'effort', None)
        return dict(requested_state=plan.logical_state, rung=binding.rung_index, role=binding.role_name,
                    agent=binding.profile_name, provider=self.where(state)['provider'], model=binding.model,
                    effort=None if effort is None else str(getattr(effort, 'value', effort)),
                    workspace=getattr(agent, 'workspace', None), shell=getattr(agent, 'shell', None),
                    capabilities=list(binding.capabilities) if binding.capabilities is not None else None,
                    timeout_seconds=getattr(profile, 'timeout_seconds', None) or getattr(agent, 'timeout', None),
                    context_mode=binding.context_mode, escalation_kind=binding.escalation_kind,
                    policy='compiled_workflow_rung', policy_identity='@v2:%s:%s:%d' % (binding.flow_name, plan.logical_state, binding.rung_index),
                    fallback_used=False)

    def budget(self, state, invocation_id):
        """The budget view of one invocation: before and after its admission (charged unless proven void)."""
        flow_state = self.compiled.flow.states[state]
        charged = invocation_id not in self.void
        plus = 1 if charged else 0
        out = dict(semantic_charged=charged)
        if self.compiled.flow.max_invocations_per_run is not None:
            out['run_invocations'] = dict(used_before=self.invocations, used_after=self.invocations+plus,
                                          limit=self.compiled.flow.max_invocations_per_run)
        out['invocation_scopes'] = [dict(scope=s.name, per_visit_of=s.per_visit_of, used_before=self.scopes.get(s.name, 0),
                                         used_after=self.scopes.get(s.name, 0)+plus, limit=s.limit) for s in flow_state.invocation_budgets]
        out['state_visits'] = dict(used=self.admitted.get(state, 0), limit=flow_state.max_state_visits_per_run)
        _, binding = self._plan(state)
        if binding is not None:
            agent = self.project.agents.get(binding.profile_name)
            out['infrastructure_failures'] = dict(agent=binding.profile_name, used=self.infra.get(binding.profile_name, 0),
                                                  limit=getattr(agent, 'infrastructure_failures', None))
        return out

    def feed(self, event, interrupted=None, live=None):
        """Trace events (type, state, visit, invocation, payload) for the next canonical event."""
        index, self.position = self.position, self.position + 1
        kind, state = event.kind, event.state
        payload, out = {}, []
        common = dict(occurred_at=_iso(event.occurred_at_unix_ns))
        if kind == 'state_entered':
            self.admitted[state] = self.admitted.get(state, 0)+1
            for scope in self.all_scopes.values():
                if scope.per_visit_of == state:
                    self.scopes[scope.name] = 0
            self.visits[event.state_visit_id] = dict(state=state, number=self.admitted[state], launches=0, attempts=0)
            flow_state = self.compiled.flow.states[state]
            payload = dict(visit_number=self.admitted[state], max_state_visits_per_run=flow_state.max_state_visits_per_run,
                           terminal=flow_state.terminal, wait_for=flow_state.wait_for)
        elif kind == 'invocation_started':
            visit = self.visits.setdefault(event.state_visit_id, dict(state=state, number=None, launches=0, attempts=0))
            visit['launches'] += 1
            charged = event.invocation_id not in self.void
            visit['attempts'] += 1 if charged else 0
            budget = self.budget(state, event.invocation_id)
            ordinals = dict(invocation_in_visit=visit['launches'], semantic_attempt=visit['attempts'] if charged else None, turn=None)
            self.started[event.invocation_id] = dict(at=event.occurred_at_unix_ns, ordinals=ordinals)
            out.append(('routing_decided', self.routing(state)))
            payload = dict(ordinals, budget=budget, **(live or {}))
            if event.invocation_id not in self.void:
                self.invocations += 1
                for scope in self.compiled.flow.states[state].invocation_budgets:
                    self.scopes[scope.name] = self.scopes.get(scope.name, 0)+1
            else:
                _, binding = self._plan(state)
                self.infra[binding.profile_name] = self.infra.get(binding.profile_name, 0)+1
        elif kind == 'invocation_finished':
            started = self.started.get(event.invocation_id, {})
            _, binding = self._plan(state)
            payload = dict(started.get('ordinals') or dict(invocation_in_visit=None, semantic_attempt=None, turn=None), outcome=event.outcome, outcome_category='semantic_outcome',
                           termination='completed', started_at=_iso(started.get('at')), finished_at=common['occurred_at'],
                           duration_ms=_ms(event.duration_ns), timeout_seconds=self.routing(state)['timeout_seconds'] if binding else None,
                           **(live or {'artifact_refs': None}))
        elif kind == 'transition_selected':
            target = self.where(event.target)
            payload = dict(from_state=self.where(state)['state'], from_visit=event.state_visit_id, to_state=target['state'],
                           to_runtime_state=event.target, to_rung=target['rung'], trigger=event.trigger_event or 'outcome',
                           outcome=event.outcome)
        elif kind == 'budget_exceeded':
            payload = dict(metric=event.metric, limit=event.limit, observed=event.observed, route_to=self.where(event.target)['state'] if event.target else None)
        elif kind in ('human_input_requested', 'human_input_received'):
            payload = dict(request_id=event.state_visit_id, target=self.where(event.target)['state'] if event.target else None)
        elif kind in ('run_resumed', 'human_input_resumed'):
            payload = dict(resume_kind='human_input' if kind == 'human_input_resumed' else 'recover_to' if event.target else 'interrupted_visit',
                           target=self.where(event.target)['state'] if event.target else None, **(live or {}))
        elif kind == 'run_finished':
            payload = dict(terminal=self.where(state)['state'], duration_ms=_ms(event.duration_ns))
        elif kind == 'run_error':
            started = self.started.get(event.invocation_id, {}) if event.invocation_id else {}
            payload = dict(started.get('ordinals') or {}, started_at=_iso(started.get('at')), **_interruption(interrupted))
        elif kind == 'run_started':
            payload = dict(live or {})
        trace_type = _KIND.get(kind)
        if trace_type is None:
            return index, []
        out.append((trace_type, payload))
        return index, [(t, state, event.state_visit_id, event.invocation_id, p, common) for t, p in out]


def _interruption(op):
    """The interruption facts the runtime recorded durably (operation `interrupted`); unknown stays unknown."""
    if not op:
        return dict(reason=None, outcome_category=None, termination='unknown', semantic_invocation=None)
    failure = op.get('provider_failure') or {}
    category = failure.get('classification') or ('timeout' if op.get('reason') == 'provider_timeout' else op.get('reason'))
    return dict(reason=op.get('reason'), outcome_category=category, semantic_invocation=op.get('semantic_invocation'),
                termination='timeout' if failure.get('timed_out') or op.get('reason') == 'provider_timeout' else
                ('provider_exit' if op.get('returncode') is not None else 'interrupted'),
                exit_code=op.get('returncode'), duration_ms=failure.get('duration_ms'), resume_safe=op.get('resume_safe'),
                diagnostic=_text(op.get('diagnostic')))


# --- Writer ---------------------------------------------------------------------------------------------
class Tracer:
    """Live and backfill writer for one `_drive`. Every public method is failure-isolated."""

    def __init__(self, root, store, compiled, project, baseline, void, baseline_digest):
        from .versioning import runtime_version
        self.root, self.store, self.dir = Path(root), store, _store_dir(root)/'events'
        iso = baseline.get('isolation') or {}
        self.work_id = work_identity(baseline, baseline_digest)
        config = baseline.get('config') or ''
        self.runtime = dict(switchyard_version=runtime_version(), config_sha256=hashlib.sha256(config.encode('utf-8')).hexdigest(),
                            flow=baseline.get('flow'))
        identity = (baseline.get('snapshot') or {}).get('identity') or {}
        self.provenance = dict(
            runtime=dict(self.runtime, baseline_runtime_version=baseline.get('runtime_version'),
                         work_schema_version=baseline.get('work_schema_version'),
                         lifecycle_contract_version=baseline.get('lifecycle_contract_version'),
                         baseline_record_sha256=baseline_digest),
            git=dict(repository=iso.get('repository') or identity.get('git_dir'), delivery_branch=iso.get('delivery_branch'),
                     delivery_base_sha=iso.get('base_sha') or identity.get('head'), source_branch=iso.get('source_branch'),
                     worktree=iso.get('delivery_worktree') or identity.get('target'), head=identity.get('head')))
        self.projector = _Projector(compiled, project, void)
        self.written = {}
        if self.dir.is_dir():
            for name in os.listdir(self.dir):
                seq, _, rest = name.partition('-')
                if name.endswith('.json') and seq.isdigit():
                    self.written[int(seq)] = rest[:-5]
        self.pending = None
        self.manifest_lines = None
        self.failures = 0

    def _id(self, index, kind, trace_type, run_id):
        return hashlib.sha256('\0'.join((self.work_id, run_id, str(index), kind, trace_type)).encode('utf-8')).hexdigest()[:32]

    def _write(self, record):
        seq, event_id = record['sequence'], record['event_id']
        if seq in self.written:
            return  # Already durable (same logical event), or a different projection: never overwritten.
        if record['event_type'] in ('invocation_finished', 'run_interrupted') and record['invocation_id']:
            # Deterministic link to an already published diagnostic (#1 slice 3A); otherwise joined on read.
            from .diagnostics import diagnostic_id, diagnostic_path
            record['payload']['diagnostic_id'] = (diagnostic_id(self.work_id, record['invocation_id'])
                                                  if diagnostic_path(self.root, record['invocation_id']).is_file() else None)
        self.dir.mkdir(parents=True, exist_ok=True)
        schema = self.dir.parent/'schema.json'
        if not schema.exists():
            _publish(schema, dict(schema_version=SCHEMA_VERSION, event_types=list(EVENT_TYPES)))
        _publish(self.dir/('%06d-%s.json' % (seq, event_id)), record)
        self.written[seq] = event_id

    def _emit(self, event, observed_by, interrupted=None, live=None, defer=False):
        index, produced = self.projector.feed(event, interrupted, live)
        records = []
        for trace_type, state, visit, invocation, payload, common in produced:
            self.projector.sequence += 1
            where = self.projector.where(state)
            records.append(dict(
                schema_version=SCHEMA_VERSION, event_type=trace_type,
                event_id=self._id(index, event.kind, trace_type, event.run_id), sequence=self.projector.sequence,
                work_id=self.work_id, run_id=event.run_id, state_visit_id=visit,
                invocation_id=invocation if trace_type in ('routing_decided', 'invocation_started', 'invocation_finished',
                                                           'transition_decided', 'run_interrupted') else None,
                state=where['state'], runtime_state=state, rung=where['rung'], agent=where['agent'],
                provider=where['provider'], model=where['model'], timestamp=common['occurred_at'],
                observed_by=observed_by, source=dict(log='events.jsonl', index=index, kind=event.kind),
                runtime=self.runtime, payload=payload))
        if defer:
            self.pending = (records, observed_by)
            return
        for record in records:
            self._write(record)

    def backfill(self, history, operations):
        """Project the durable canonical history; write only events that are missing (proven by that history)."""
        try:
            for event in history:
                interrupted = None
                if event.kind == 'run_error' and event.invocation_id:
                    interrupted = next((op for op in operations if op.get('phase') == 'interrupted'
                                        and op.get('invocation_id') == event.invocation_id), None)
                live = dict(provenance=self.provenance) if event.kind == 'run_started' else None
                self._emit(event, 'recovery_backfill', interrupted, live)
        except Exception:
            self.failures += 1
            self.projector = None

    def record(self, event):
        """Called after the canonical event is durable. `run_error` waits for its interruption record."""
        if self.projector is None:
            return
        try:
            live = None
            if event.kind == 'run_started':
                live = dict(provenance=self.provenance)
            elif event.kind == 'invocation_started':
                self.manifest_lines = self._manifest_lines()
                live = dict(git=dict(worktree=self.provenance['git']['worktree'], head=self.provenance['git']['head']))
            elif event.kind == 'invocation_finished':
                live = dict(artifact_refs=self._artifact_refs())
            elif event.kind == 'run_resumed':
                live = self._resume_context()
            self._emit(event, 'runtime', live=live, defer=event.kind == 'run_error')
        except Exception:
            self.failures += 1
            self.projector = None  # Stop tracing this drive; never affect execution.

    def interrupted(self, operation):
        """The runtime recorded why the run stopped: complete the deferred `run_interrupted` event."""
        try:
            if self.pending is None:
                return
            records, observed_by = self.pending
            self.pending = None
            for record in records:
                record['payload'].update(_interruption(operation))
                self._write(record)
        except Exception:
            self.failures += 1

    # Live-only enrichment (values the runtime has at hand; never reconstructed later).
    def _manifest_lines(self):
        path = self.root/'manifest.jsonl'
        return path.read_bytes().count(b'\n') if path.is_file() else 0

    def _artifact_refs(self):
        if self.manifest_lines is None:
            return None
        refs = manifest_refs(self.root, self.manifest_lines)
        self.manifest_lines = None
        return refs

    def _resume_context(self):
        op = json.loads(self.store.read_artifact('operation'))
        recovery = op.get('recovery') or {}
        out = {k: recovery[k] for k in ('from_terminal', 'resume_to', 'qa_fix_round') if k in recovery}
        if self.store.versions('qa_reference'):
            reference = json.loads(self.store.read_artifact('qa_reference'))
            out['candidate'] = dict(identity=reference.get('identity'))
        if op.get('inferred_interruption'):
            out['inferred_interruption'] = {k: op['inferred_interruption'].get(k) for k in ('invocation_id', 'semantic_invocation')}
        return dict(context=out)


def work_identity(baseline, baseline_digest):
    """Durable work identity: the delivery branch suffix; non-isolated works: the baseline record digest."""
    iso = baseline.get('isolation') or {}
    return iso['delivery_branch'][len('refs/heads/switchyard/'):] if iso.get('delivery_branch') else 'baseline-'+baseline_digest[:16]


def manifest_refs(root, start):
    """References (never copies) to the artifact manifest records appended after line `start`."""
    lines = (Path(root)/'manifest.jsonl').read_bytes().decode('utf-8').splitlines()[start:]
    refs = []
    for line in lines:
        record = json.loads(line)
        if record.get('kind') == 'artifact':
            refs.append(dict(kind='artifact', name=record['name'], version=record['version'], path=record['path'],
                             sha256=record['sha256'], byte_size=record['byte_size']))
        elif record.get('kind') == 'handoff':
            refs.append(dict(kind='handoff', sequence=record['sequence'], path=record['path'], sha256=record['sha256']))
    return refs[:MAX_REFS] + ([dict(kind='truncated', omitted=len(refs)-MAX_REFS)] if len(refs) > MAX_REFS else [])


def _publish(path, value):
    """Crash-safe immutable publication: fsynced temporary, exclusive hard link (never overwrites)."""
    data = (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))+'\n').encode('utf-8')
    temporary = path.parent/('.tmp-%d-%s' % (os.getpid(), path.name))
    with open(temporary, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink()


def tracer(root, store, compiled, project, void):
    """A Tracer for this drive, or None when one cannot be built (tracing is then simply off)."""
    try:
        latest = store.latest_artifact('baseline')
        return Tracer(root, store, compiled, project, json.loads(store.read_artifact('baseline')), void, latest.sha256)
    except Exception:
        return None


# --- Read API -------------------------------------------------------------------------------------------
def trace_events(work_root, run_id=None, state=None, invocation_id=None, event_type=None):
    """The work's trace events in sequence order, filtered. Read-only: never writes, repairs, runs Git or
    contacts a provider or remote. A work without an observability store has no trace events ([])."""
    directory = _store_dir(Path(work_root))/'events'
    if not directory.is_dir():
        return []
    events = []
    for name in sorted(os.listdir(directory)):
        seq, _, rest = name.partition('-')
        if not (name.endswith('.json') and seq.isdigit()):
            continue
        record = json.loads((directory/name).read_text(encoding='utf-8'))
        if type(record) is not dict or record.get('schema_version') != SCHEMA_VERSION or record.get('sequence') != int(seq):
            continue
        if ((run_id is None or record['run_id'] == run_id) and (state is None or state in (record['state'], record['runtime_state']))
                and (invocation_id is None or record['invocation_id'] == invocation_id)
                and (event_type is None or record['event_type'] == event_type)):
            events.append(record)
    return events


def trace_summary(work_root):
    """Tiny local summary for `recovery_status` (None without a trace)."""
    directory = _store_dir(Path(work_root))/'events'
    if not directory.is_dir():
        return None
    names = sorted(n for n in os.listdir(directory) if n.endswith('.json') and n.partition('-')[0].isdigit())
    if not names:
        return None
    last = json.loads((directory/names[-1]).read_text(encoding='utf-8'))
    return dict(trace_events=len(names), last_sequence=last.get('sequence'), last_event=last.get('event_type'))
