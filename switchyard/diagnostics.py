"""OBSERVABILITY (#1) slice 3A: durable, bounded provider diagnostics, one record per invocation.

A diagnostic explains WHY one invocation (`invocation_id`, the runtime's own identity) ended as it did. It is
observational evidence, never authority: `events.jsonl`, the recovery operation records and the artifact manifest
stay authoritative, and nothing here is read by execution. Every entry point swallows its own errors; a failed
write leaves the invocation without a diagnostic and changes nothing else (no retry, no charge, no lifecycle move).

Sources (no new taxonomy): process metadata observed by `provider_process.execute`; documented structured provider
fields (Claude result envelope) recorded by the adapter; the StageOutputContract errors the parser already produces;
the existing failure classification (`recovery._classify`) and the `interrupted` operation record (reason,
semantic_invocation, provider_failure) for runs that stopped.

Streams (see docs/design/provider-diagnostics.md): no unstructured provider stream is ever retained. stdout is the
result channel (generated code, prompts, structured payloads); LLM-provider stderr (Claude, Codex) is arbitrary text no
sanitizer can prove free of source or prompts: only its byte count is kept. Verifier output is evidence already kept as
a declared artifact. Structured provider errors (documented Claude envelope fields) are kept, each message through the
sanitization gate below, which rejects rather than partially redacts.

Storage: `<work>/observability/diagnostics/<invocation id, ':' -> '_'>.json`, one immutable record published with an
exclusive link (never overwritten). Recovery backfill writes only what canonical records prove (`partial`).
"""
import hashlib
import json
import os
from pathlib import Path
import re

from .provider_process import ProviderExecutionError
from .provider_stages import _ANSI, _CONTROL, _SECRETS
from .trace import _publish, manifest_refs, work_identity

SCHEMA_VERSION = 1
MAX_SUMMARY = 2048
MAX_ERRORS = 20
MAX_RECORD = 32768
MAX_MESSAGES = 16

# Prompt/section headers of render_invocation and StageResult payload keys: their presence means the stream
# echoes provider input or output, which must never be retained.
_PROMPT_MARKERS = ('SWITCHYARD EXECUTION CONTRACT', 'EXECUTION BINDING', 'DURABLE INPUT ARTIFACTS', 'STAGE OUTPUT CONTRACT',
                   'OUTPUT CONTRACT', 'STRUCTURED OUTPUT FIELDS', 'PRIOR REASONING HANDOFF', 'RESOLVED REQUEST SOURCES',
                   'MANUAL QA', 'HUMAN CLARIFICATION', 'VERIFIER DIAGNOSTICS', 'AVAILABLE EXECUTION CAPABILITIES')
_PAYLOAD_MARKERS = ('"produced_artifacts"', '"structured_output"', '"handoff"', '"content"', '"diagnostics"')
_UNSAFE = re.compile(r'-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)|^\s*(?:proxy-)?authorization\s*:|'
                     r'^\s*(?:set-)?cookie\s*:|\b[a-z][a-z0-9+.-]*://[^/\s@]+@', re.I | re.M)
_REDACT = (*_SECRETS,
           (re.compile(r'\b(?:gh[pousr]|github_pat|xox[abposr]|glpat)[_-][A-Za-z0-9_]{8,}'), '[REDACTED]'),
           (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '[REDACTED]'))
_ENV_LINE = re.compile(r'(?m)^\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}=\S')
_OPAQUE = re.compile(r'[A-Za-z0-9+/_\-]{32,}={0,2}')
_OMITTED = 'unsafe_unstructured_output'


def _clean(text):
    return _CONTROL.sub(' ', _ANSI.sub('', text))


def _opaque(token):
    return any(c.isupper() for c in token) and any(c.islower() for c in token) and any(c.isdigit() for c in token)


def sanitize(text):
    """(text, None) when the unstructured text is safe to retain, else (None, reason).

    Redaction alone is not trusted: any credential-looking match, an environment dump, an opaque token, a key or
    an echoed prompt/payload rejects the whole text rather than retaining a partially redacted one.
    """
    text = _clean(text).strip()
    if not text:
        return None, 'no_output'
    if any(marker in text for marker in _PROMPT_MARKERS + _PAYLOAD_MARKERS) or _UNSAFE.search(text):
        return None, _OMITTED
    if any(pattern.search(text) for pattern, _ in _REDACT):
        return None, _OMITTED
    if len(_ENV_LINE.findall(text)) >= 3 or any(_opaque(m.group()) for m in _OPAQUE.finditer(text)):
        return None, _OMITTED
    return text, None


def _bounded(text, limit):
    """Code-owned text, still credential-stripped, clipped to `limit` UTF-8 bytes (head kept)."""
    if text is None:
        return None
    text = _clean(str(text)).strip()
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    data = text.encode('utf-8')
    return text if len(data) <= limit else data[:limit-3].decode('utf-8', errors='ignore')+'...'


def file_name(invocation_id):
    return invocation_id.replace(':', '_')+'.json'


def _directory(root):
    return Path(root)/'observability'/'diagnostics'


def diagnostic_path(root, invocation_id):
    return _directory(root)/file_name(invocation_id)


def diagnostic_id(work_id, invocation_id):
    return hashlib.sha256('\0'.join((work_id, invocation_id, 'diagnostic')).encode('utf-8')).hexdigest()[:32]


def _classify(error):
    from .recovery import _classify as classify  # The one existing failure taxonomy.
    return classify(error)


class Diagnostics:
    """Live and backfill writer for one `_drive`. Every public method is failure-isolated."""

    def __init__(self, root, work_id):
        self.root, self.work_id = Path(root), work_id
        self.compiled = None
        self.facts = None      # InvocationFacts of the invocation that just returned or raised
        self.current = None    # (invocation_started event, manifest line count)
        self.pending = None    # run_error awaiting its `interrupted` operation record
        self.failures = 0

    def bind(self, compiled):
        self.compiled = compiled

    # --- live --------------------------------------------------------------------------------------------
    def captured(self, facts):
        self.facts = facts

    def record(self, event):
        """Called after the canonical event is durable (before the trace sees it)."""
        try:
            if event.kind == 'invocation_started':
                path = self.root/'manifest.jsonl'
                self.current = (event, path.read_bytes().count(b'\n') if path.is_file() else 0)
                self.facts = self.pending = None
            elif event.kind == 'invocation_finished' and self._current(event):
                try:
                    refs = manifest_refs(self.root, self.current[1])
                except Exception:
                    refs = None  # Unknown, not empty.
                try:
                    self._write(self._build(event, self.facts, 'runtime', outcome=event.outcome, refs=refs,
                                            duration_ns=event.duration_ns))
                finally:
                    self._usage(event, self.facts, 'runtime')
                    self.current = self.facts = None
            elif event.kind == 'run_error' and event.invocation_id and self._current(event):
                self.pending = (event, self.facts)
        except Exception:
            self.failures += 1

    def interrupted(self, operation):
        """The runtime recorded why the run stopped: finalize the pending invocation's diagnostic."""
        try:
            if self.pending is None:
                return
            (event, facts), self.pending = self.pending, None
            if operation.get('invocation_id') == event.invocation_id:
                try:
                    self._write(self._build(event, facts, 'runtime', op=operation))
                finally:
                    self._usage(event, facts, 'runtime')
        except Exception:
            self.failures += 1

    def _usage(self, event, facts, observed_by):
        """The invocation's usage record (ECONOMICS-001), isolated from the diagnostic and from execution."""
        try:
            from . import usage
            _, _, where = self._where(event.state)
            record = usage.build(self.work_id, event, where, facts, observed_by, where['model'])
        except Exception:
            self.failures += 1
            return
        try:
            usage.write(self.root, record)
        except Exception:
            self.failures += 1
        self._costs(event, record, (getattr(facts, 'usage', None) or {}).get('pricing_detail'), observed_by)

    def _costs(self, event, record, pricing_detail, observed_by):
        """Frozen pricing snapshot and cost assessment (ECONOMICS-004), isolated from usage and from execution."""
        try:
            from . import economics
            if observed_by == 'recovery_backfill':
                economics.backfill(self.root, self.work_id, record)
            else:
                economics.publish(self.root, self.work_id, event, record, pricing_detail, observed_by)
        except Exception:
            self.failures += 1

    def _current(self, event):
        return self.current is not None and self.current[0].invocation_id == event.invocation_id

    # --- recovery ----------------------------------------------------------------------------------------
    def backfill(self, history, operations):
        """Write missing diagnostics that canonical records prove: a finished invocation, or an invocation whose
        run_error has its `interrupted` operation record. Nothing else is reconstructed (no streams, no process
        metadata the records do not hold); an invocation interrupted without a record keeps no diagnostic."""
        try:
            existing = set(os.listdir(_directory(self.root))) if _directory(self.root).is_dir() else set()
            started = {}
            for event in history:
                if event.kind == 'invocation_started':
                    started[event.invocation_id] = event
                    continue
                if event.kind not in ('invocation_finished', 'run_error') or event.invocation_id not in started:
                    continue
                if file_name(event.invocation_id) in existing:
                    continue
                if event.kind == 'invocation_finished':
                    record = self._build(event, None, 'recovery_backfill', outcome=event.outcome, duration_ns=event.duration_ns)
                else:
                    op = next((op for op in operations if op.get('phase') == 'interrupted'
                               and op.get('invocation_id') == event.invocation_id), None)
                    if op is None:
                        continue
                    record = self._build(event, None, 'recovery_backfill', op=op)
                self._write(record)
                existing.add(file_name(event.invocation_id))
        except Exception:
            self.failures += 1
        self._backfill_usage(history, operations)

    def _backfill_usage(self, history, operations):
        """Missing usage records for invocations with a proven end, as `unavailable`/`not_captured`: no durable
        record holds provider usage, so nothing is reconstructed and no provider is rerun."""
        try:
            from . import usage
            directory = usage._directory(self.root)
            existing = set(os.listdir(directory)) if directory.is_dir() else set()
            started = set()
            for event in history:
                if event.kind == 'invocation_started':
                    started.add(event.invocation_id)
                    continue
                if event.kind not in ('invocation_finished', 'run_error') or event.invocation_id not in started:
                    continue
                if usage.file_name(event.invocation_id) in existing:
                    continue
                if event.kind == 'run_error' and not any(op.get('phase') == 'interrupted' and op.get('invocation_id') == event.invocation_id
                                                         for op in operations):
                    continue
                self._usage(event, None, 'recovery_backfill')
                existing.add(usage.file_name(event.invocation_id))
        except Exception:
            self.failures += 1
        self._backfill_costs(history, operations)

    def _backfill_costs(self, history, operations):
        """Missing assessments for ended invocations that have a durable usage record: computed only from that record
        and the invocation's own frozen pricing snapshot (`pricing_not_captured` without one). Never today's prices."""
        try:
            from . import economics, usage
            directory = economics._directory(self.root, 'costs')
            existing = set(os.listdir(directory)) if directory.is_dir() else set()
            started = set()
            for event in history:
                if event.kind == 'invocation_started':
                    started.add(event.invocation_id)
                    continue
                if event.kind not in ('invocation_finished', 'run_error') or event.invocation_id not in started:
                    continue
                if economics.file_name(event.invocation_id) in existing:
                    continue
                try:
                    record = json.loads((usage._directory(self.root)/usage.file_name(event.invocation_id)).read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                if type(record) is dict and record.get('invocation_id') == event.invocation_id:
                    self._costs(event, record, None, 'recovery_backfill')
                    existing.add(economics.file_name(event.invocation_id))
        except Exception:
            self.failures += 1

    # --- record ------------------------------------------------------------------------------------------
    def _where(self, state):
        plan = self.compiled.plan.get(state) if self.compiled is not None and state is not None else None
        binding = plan.binding if plan is not None else None
        profile = self.compiled.profiles.get(binding.profile_name) if binding is not None else None
        return binding, profile, dict(
            state=plan.logical_state if plan is not None else state, runtime_state=state,
            rung=binding.rung_index if binding is not None else None,
            agent=binding.profile_name if binding is not None else None,
            provider=str(getattr(binding.provider, 'value', binding.provider)) if binding is not None else None,
            model=binding.model if binding is not None else None)

    def _build(self, event, facts, observed_by, outcome=None, op=None, refs=None, duration_ns=None):
        binding, profile, where = self._where(event.state)
        provider = where['provider']
        process = dict(facts.process) if facts is not None and facts.process is not None else None
        error = facts.error if facts is not None else None
        stage_outcome = outcome if outcome is not None else (facts.result if facts is not None else None)
        failure = (op or {}).get('provider_failure') or {}
        classification = failure.get('classification')
        if classification is None and isinstance(error, ProviderExecutionError):
            classification = _classify(error)
        if facts is not None:
            produced = facts.result is not None
        elif outcome is not None:
            produced = True
        elif op is not None and op.get('semantic_invocation') is False:
            produced = False  # Proven: the interruption was voided as ending without a StageResult.
        else:
            produced = None
        if process is not None:
            process.pop('stderr_stream', None)
            process['launches'] = facts.launches
        elif op is not None and observed_by == 'recovery_backfill':
            # Only what the durable interruption record holds; everything else stays unknown (null).
            process = dict(launched=None, exit_code=op.get('returncode'), termination_signal=None, terminated_by=None,
                           timed_out=failure['timed_out'] if 'timed_out' in failure else (True if op.get('reason') == 'provider_timeout' else None),
                           duration_ms=failure.get('duration_ms'), source='interrupted_operation')
        timeout = process.get('timeout_seconds') if process and process.get('timeout_seconds') is not None else \
            getattr(profile, 'timeout_seconds', None)
        if process is not None and process.get('duration_ms') is not None and process.get('source') is None:
            duration, duration_source = process['duration_ms'], 'process'
        elif duration_ns is not None:
            duration, duration_source = duration_ns//1_000_000, 'canonical_event'
        elif failure.get('duration_ms') is not None:
            duration, duration_source = failure['duration_ms'], 'interrupted_operation'
        else:
            duration, duration_source = None, None
        truncated = False
        excerpt = self._excerpt(provider, facts)
        channel = None
        if facts is not None and facts.channel is not None:
            channel = dict(facts.channel)
            messages, omitted = [], 0
            for message in channel.pop('errors', None) or ():
                safe, _ = sanitize(message)
                if safe is None:
                    omitted += 1
                else:
                    messages.append(_bounded(safe, 512))
            if messages:
                channel['errors'] = messages[:MAX_MESSAGES]
            if omitted:
                channel['errors_omitted'] = dict(count=omitted, reason=_OMITTED)
        structured = self._structured(facts, classification, produced)
        if structured is not None and structured.get('errors_truncated'):
            truncated = True
        verification = self._verification(binding, profile, process, stage_outcome, refs) if provider == 'command' else None
        if refs and refs[-1].get('kind') == 'truncated':
            truncated = True
        result = dict(produced_stage_result=produced, outcome=_bounded(stage_outcome, 64) if stage_outcome is not None else None,
                      outcome_category='semantic_outcome' if outcome is not None else (classification or (op or {}).get('reason')),
                      classification=classification, reason=(op or {}).get('reason'),
                      semantic_invocation=True if outcome is not None else (op or {}).get('semantic_invocation'),
                      resume_safe=(op or {}).get('resume_safe'),
                      error=_bounded(str(error), MAX_SUMMARY) if error is not None else
                      (_bounded(op.get('diagnostic'), MAX_SUMMARY) if op is not None else None))
        complete = observed_by == 'runtime' and facts is not None and (process is not None or error is not None)
        record = dict(
            schema_version=SCHEMA_VERSION, diagnostic_id=diagnostic_id(self.work_id, event.invocation_id),
            work_id=self.work_id, run_id=event.run_id, state_visit_id=event.state_visit_id, invocation_id=event.invocation_id,
            **where, observed_by=observed_by, completeness='complete' if complete else 'partial',
            timeout_seconds=timeout, duration_ms=duration, duration_source=duration_source,
            result=result, process=process, provider_channel=channel, stderr_excerpt=excerpt,
            structured_output=structured, verification=verification,
            artifact_refs=refs, sources=dict(events='events.jsonl', operation='interrupted' if op is not None else None))
        record['summary'] = _bounded(_summary(record, provider), MAX_SUMMARY)
        record['truncated'] = truncated
        return _fit(record)

    def _excerpt(self, provider, facts):
        out = dict(stream='stderr', available=False)
        if facts is None or facts.process is None:
            return dict(out, reason='not_captured')
        if provider == 'command' or facts.process.get('stderr_stream') != 'separate':
            return dict(out, reason='verifier_output_kept_as_evidence_artifact')
        # Unstructured LLM-provider stderr (Claude and Codex alike) is never persisted: no sanitizer can prove it
        # free of source, prompts or model output. Only its size is kept; structured errors live in provider_channel.
        return dict(out, reason='unstructured_output_not_retained', bytes_seen=facts.stderr_bytes)

    def _structured(self, facts, classification, produced):
        if facts is None or facts.structured is None:
            return None
        structured = facts.structured
        errors = list(structured.get('errors') or getattr(facts.error, 'errors', None) or ())
        repair = dict(structured.get('repair') or {})
        invalid = classification == 'invalid_stage_result'
        return dict(schema=structured['schema'], valid=True if produced else (False if invalid else None),
                    error=_bounded(str(facts.error), MAX_SUMMARY) if invalid and facts.error is not None else None,
                    errors=[{k: (v if k == 'allowed_fields' else _bounded(v, 256)) for k, v in e.items()} for e in errors[:MAX_ERRORS]],
                    errors_truncated=len(errors) > MAX_ERRORS, repair=repair, raw_result_retained=False)

    def _verification(self, binding, profile, process, outcome, refs):
        command = getattr(profile, 'command', None)
        if command is None:
            return None
        code = (process or {}).get('exit_code')
        rule = None if code is None else ('declared_exit_code' if code in command.outcomes.exit_codes else 'default_outcome')
        return dict(verifier=binding.profile_name, executable=os.path.basename(command.argv[0]), argc=len(command.argv),
                    argv_sha256=hashlib.sha256(json.dumps(list(command.argv)).encode('utf-8')).hexdigest(),
                    exit_code=code, timed_out=(process or {}).get('timed_out'), outcome=outcome, outcome_rule=rule,
                    evidence_artifact=command.capture.artifact if command.capture is not None else None,
                    evidence_refs=[r for r in refs or () if r.get('kind') == 'artifact'] if refs is not None else None)

    def _write(self, record):
        directory = _directory(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        schema = directory/'schema.json'
        if not schema.exists():
            _publish(schema, dict(schema_version=SCHEMA_VERSION, record='invocation_diagnostic',
                                  limits=dict(summary=MAX_SUMMARY, errors=MAX_ERRORS, record=MAX_RECORD)))
        path = directory/file_name(record['invocation_id'])
        if path.exists():
            return  # Finalized records are immutable; a retried recovery never overwrites one.
        _publish(path, record)


def _summary(record, provider):
    r, p = record['result'], record['process'] or {}
    who = 'Verifier' if provider == 'command' else 'Provider'
    if r['produced_stage_result'] and provider == 'command':
        v = record['verification'] or {}
        text = "Verifier exited with status %s; outcome '%s' (%s)." % (v.get('exit_code'), r['outcome'], (v.get('outcome_rule') or 'unknown rule').replace('_', ' '))
    elif r['produced_stage_result']:
        text = "Provider returned a valid stage result with outcome '%s'." % r['outcome']
    elif r['produced_stage_result'] is None and r['outcome_category'] is None:
        text = 'Outcome unknown: no durable record explains this invocation.'
    elif p.get('timed_out'):
        text = '%s timed out after %ss and was killed by Switchyard; no stage result was produced.' % (who, record['timeout_seconds'])
    elif p.get('launched') is False:
        text = '%s executable could not be started; no process ran.' % who
    elif p.get('termination_signal'):
        text = '%s process was terminated by %s before producing a stage result.' % (who, p['termination_signal'])
    elif r['classification'] == 'invalid_stage_result':
        s = record['structured_output'] or {}
        if (s.get('repair') or {}).get('attempted'):
            text = 'Provider exhausted structured-output repair (%s attempts in one invocation) without a valid stage result.' % s['repair'].get('max_attempts')
        else:
            text = 'Provider %s but its structured result was invalid: %s.' % (
                'exited normally' if p.get('exit_code') == 0 else 'ended', r['error'] or 'contract violation')
    elif p.get('exit_code') not in (None, 0):
        text = '%s exited with status %s before producing a valid stage result.' % (who, p['exit_code'])
    else:
        text = 'Interrupted (%s): %s' % (r['reason'] or r['outcome_category'], r['error'] or 'no further detail recorded')
    if r['produced_stage_result'] and r['reason']:
        text += ' The runtime then stopped (%s): %s' % (r['reason'], r['error'] or '')
    return text


def _size(record):
    return len(json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('utf-8'))


def _fit(record):
    """Total record bound: drop trailing errors/messages/refs; always visibly truncated."""
    if _size(record) <= MAX_RECORD:
        return record
    record['truncated'] = True
    for section, key in (('structured_output', 'errors'), ('provider_channel', 'errors'), (None, 'artifact_refs')):
        while _size(record) > MAX_RECORD:
            holder = record if section is None else record.get(section)
            items = (holder or {}).get(key)
            if not items:
                break
            items.pop()
            if section == 'structured_output':
                holder['errors_truncated'] = True
    if _size(record) > MAX_RECORD:
        record['summary'] = _bounded(record['summary'], 256)
        record['result']['error'] = _bounded(record['result']['error'], 256)
    return record


def writer(root, store):
    """A Diagnostics writer for this drive, or None when one cannot be built (diagnostics are then off)."""
    try:
        latest = store.latest_artifact('baseline')
        return Diagnostics(root, work_identity(json.loads(store.read_artifact('baseline')), latest.sha256))
    except Exception:
        return None


# --- Read API -----------------------------------------------------------------------------------------------
def _invocations(root):
    """Canonical invocation order (events.jsonl), each invocation's location from the trace when present, and the
    proven consequences: invocation I -> the first invocation of the visit that I's own outcome transition entered
    (transition_selected with I's invocation_id, then state_entered of its target, then invocation_started)."""
    from .observability import read_events
    from .trace import trace_events
    order, links, entered, pending = [], {}, {}, None
    try:
        for event in read_events(root/'events.jsonl'):
            if event.kind == 'invocation_started':
                order.append(event)
                source = entered.pop(event.state_visit_id, None)
                if source is not None:
                    links[source] = event.invocation_id
            elif event.kind == 'transition_selected':
                pending = (event.invocation_id, event.target) if event.invocation_id else None
            elif event.kind == 'state_entered':
                if pending is not None and pending[1] == event.state:
                    entered[event.state_visit_id] = pending[0]
                pending = None
            elif event.kind != 'state_exited':
                pending = None  # Refused, resumed, waiting or ended: the transition entered no visit.
    except (ValueError, OSError):
        pass
    located = {e['invocation_id']: e for e in trace_events(root, event_type='invocation_started')}
    return order, located, links


def _match(invocation_id, wanted):
    if wanted is None:
        return True
    short = wanted[1:] if wanted[:1] in ('I', 'i') and wanted[1:].isdigit() else wanted
    return invocation_id == wanted or (short.isdigit() and invocation_id.endswith(':invocation:'+short))


def read_diagnostics(work_root, invocation_id=None):
    """Invocation diagnostics in canonical invocation order. Read-only: never writes, backfills, runs Git or
    contacts a provider. An invocation without a record is listed as `completeness: unavailable` (not persisted).
    `invocation_id` may be the full id or its short form `I<n>`/`<n>`."""
    root = Path(work_root)
    directory = _directory(root)
    stored = {}
    if directory.is_dir():
        for name in sorted(os.listdir(directory)):
            if not name.endswith('.json') or name == 'schema.json':
                continue
            record = json.loads((directory/name).read_text(encoding='utf-8'))
            if type(record) is dict and record.get('schema_version') == SCHEMA_VERSION and name == file_name(record.get('invocation_id', '')):
                stored[record['invocation_id']] = dict(record, recorded=True)
    order, located, links = _invocations(root)
    out = []
    for event in order:
        record = stored.pop(event.invocation_id, None)
        if record is None:
            where = located.get(event.invocation_id) or {}
            record = dict(schema_version=SCHEMA_VERSION, diagnostic_id=None, work_id=where.get('work_id'), run_id=event.run_id,
                          state_visit_id=event.state_visit_id, invocation_id=event.invocation_id,
                          state=where.get('state', event.state), runtime_state=event.state, rung=where.get('rung'),
                          agent=where.get('agent'), provider=where.get('provider'), model=where.get('model'),
                          completeness='unavailable', reason='no_diagnostic_recorded', recorded=False, result=None,
                          summary='No diagnostic was recorded for this invocation (the controller may have stopped while it ran).')
        out.append(record)
    out.extend(stored.values())  # Records without canonical events (should not happen): still shown, never hidden.
    by_id = {r['invocation_id']: r for r in out}
    for record in out:
        if record.get('provider') != 'command':
            verifier = by_id.get(links.get(record['invocation_id']))
            record['followed_by_verification'] = None if verifier is None or verifier.get('provider') != 'command' else dict(
                invocation_id=verifier['invocation_id'], verifier=verifier.get('agent'),
                outcome=(verifier.get('result') or {}).get('outcome'),
                exit_code=(verifier.get('verification') or {}).get('exit_code'),
                basis='outcome_transition')
    return [r for r in out if _match(r['invocation_id'], invocation_id)]


def failed(record):
    result = record.get('result')
    if result is None:
        return record.get('completeness') == 'unavailable'
    if result.get('produced_stage_result') is not True or result.get('reason'):
        return True
    return record.get('provider') == 'command' and (record.get('verification') or {}).get('exit_code') not in (0, None)
