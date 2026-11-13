"""OBSERVABILITY (#1) slice 2: `work logs` and `work watch`, a presentation layer over the durable trace.

Nothing here executes, recovers, reconciles or writes. The data path is the existing one:
events.jsonl -> durable trace (observability/events) -> this renderer, with diagnostics joined by invocation id.

- TraceCursor   incremental, sequence-ordered reader of the trace: lists the event directory, parses only files past
                the cursor, and advances only over a contiguous prefix (a gap or an unreadable file holds it back;
                nothing is skipped). The directory mtime is checked first, so an idle poll lists and parses nothing.
- observed_status  pure status reader: the latest recovery `operation` record (and a recorded QA decision), read
                directly from the store manifests. Unlike `recovery_status` it never takes the controller lock and
                never reconciles a stale RUNNING record (both of which write or could block a starting controller).
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

MIN_INTERVAL, MAX_INTERVAL = 0.2, 60.0
GAP_PATIENCE = 5  # polls a final status waits for a missing trace sequence before reporting it
_RUN_LEVEL = ('run_started', 'run_resumed', 'run_finished')


# --- Incremental trace reader ---------------------------------------------------------------------------------
class TraceCursor:
    def __init__(self, work_root):
        self.dir = Path(work_root)/'observability'/'events'
        self.next = 1           # next sequence to deliver
        self.waiting = None     # sequence the cursor is held at although later events exist (gap/unreadable)
        self._mtime = None
        self._names = {}        # sequence -> file name, for sequences >= next

    def poll(self):
        """Newly available events in sequence order (the contiguous prefix only)."""
        try:
            mtime = self.dir.stat().st_mtime_ns
        except FileNotFoundError:
            return []
        if mtime != self._mtime or self.waiting is not None:
            self._mtime = mtime
            for name in os.listdir(self.dir):
                seq, _, rest = name.partition('-')
                if name.endswith('.json') and seq.isdigit() and int(seq) >= self.next:
                    self._names[int(seq)] = name  # Hidden publication temporaries never match.
        out = []
        while self.next in self._names:
            try:
                record = json.loads((self.dir/self._names[self.next]).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                break  # Not (yet) readable: retry on the next poll, never skip.
            if type(record) is not dict or record.get('sequence') != self.next or record.get('schema_version') != 1:
                break
            out.append(record)
            del self._names[self.next]
            self.next += 1
        self.waiting = self.next if self._names else None
        return out


# --- Pure status reader ---------------------------------------------------------------------------------------
MAX_STORE_RECORD = 8 << 20
_STORE_PATH = re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*')


def read_regular(path, limit):
    """Bytes of a regular, non-symlinked file of at most `limit` bytes; None otherwise (never follows a link)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        data = stream.read(limit+1)
    return data if len(data) <= limit else None


def latest_record(store, name):
    """(manifest record, value) of the latest `name` record of a work store; (None, None) when absent or
    unverifiable. Only manifest-relative paths inside the store, regular files, no symlinks, bounded size."""
    data = read_regular(Path(store)/'manifest.jsonl', MAX_STORE_RECORD) if not Path(store).is_symlink() else None
    if data is None:
        return None, None
    latest = None
    for line in data.decode('utf-8').splitlines():
        record = json.loads(line) if line else {}
        if record.get('kind') == 'artifact' and record.get('name') == name:
            latest = record
    path = latest.get('path') if latest else None
    if type(path) is not str or not _STORE_PATH.fullmatch(path) or '..' in path.split('/'):
        return None, None
    current = Path(store)
    for part in path.split('/')[:-1]:
        current = current/part
        if current.is_symlink():
            return None, None
    data = read_regular(Path(store)/path, MAX_STORE_RECORD)
    if data is None or hashlib.sha256(data).hexdigest() != latest.get('sha256'):
        return None, None
    return latest, json.loads(data)


def _latest(store, name):
    """The latest `name` record of a work store, read from its manifest; None when absent or unverifiable."""
    return latest_record(store, name)[1]


_ACTIVE = ('provisioning', 'running', 'restoring', 'suspending')  # A controller operation is in progress.
_NAMES = {'running': 'RUNNING', 'interrupted': 'INTERRUPTED', 'reopened': 'REOPENED', 'rolled_back': 'ROLLED_BACK',
          'rollback_running': 'ROLLBACK_BLOCKED', 'accepted': 'ACCEPTED', 'suspending': 'SUSPENDING',
          'suspended': 'SUSPENDED', 'restoring': 'RESTORING', 'provisioning': 'PROVISIONING',
          'waiting_for_human': 'WAITING_FOR_HUMAN'}  # The recovery_status terminal names.


def observed_status(work_root):
    """{status, phase, active, next_action, note} from durable records only; read-only, never reconciling."""
    root = Path(work_root)
    try:
        op = _latest(root/'recovery', 'operation')
    except (OSError, ValueError):
        op = None
    if op is None:
        return dict(status='UNKNOWN', phase=None, active=False, next_action=None,
                    note='no readable recovery record (legacy work, or not a work directory)')
    phase, w = op.get('phase'), str(root)
    status = _NAMES.get(phase) or (op.get('terminal') if phase == 'delivery_done' else None) or str(phase).upper()
    out = dict(status=status, phase=phase, active=phase in _ACTIVE, next_action=None, note=None, state=op.get('state'))
    if phase == 'running' and op.get('provider_pid') is not None:
        try:
            os.kill(op['provider_pid'], 0)
        except ProcessLookupError:
            out['note'] = ('the recorded provider process %s is not alive but the work still records RUNNING; the controller '
                           'may have stopped (`switchyard work status %s` reconciles; it writes)' % (op['provider_pid'], w))
        except PermissionError:
            pass
    elif phase == 'waiting_for_human':
        out['next_action'] = 'answer the clarification: switchyard work answer %s %s "<answer>"' % (w, op.get('request_id'))
    elif phase == 'interrupted':
        out['note'] = op.get('reason')
        out['next_action'] = ('resume available: switchyard work resume %s' % w if op.get('resume_safe') else
                              'resume not available; inspect: switchyard work status %s' % w)
    elif phase == 'delivery_done':
        if status == 'READY_FOR_MANUAL_QA':
            try:
                reference = _latest(root/'recovery', 'qa_reference')
                decision = reference and _latest(reference['workspace'], 'qa_decision')
            except (OSError, ValueError, KeyError, TypeError):
                decision = None
            if decision and decision.get('terminal'):
                out['status'] = decision['terminal']
        out['next_action'] = ('manual QA: review the candidate, then switchyard work accept %s or switchyard work qa-fix %s "<feedback>"'
                              % (w, w) if out['status'] == 'READY_FOR_MANUAL_QA' else 'inspect: switchyard work status %s' % w)
    elif phase == 'reopened':
        out['next_action'] = 'QA rejection recorded; continue: switchyard work qa-fix %s "<feedback>"' % w
    elif phase == 'suspended':
        out['next_action'] = 'resume when ready: switchyard work resume %s' % w
    elif phase == 'accepted':
        out['next_action'] = 'integrate: switchyard work integration-status %s' % w
    elif phase not in _NAMES:
        out['note'] = 'unrecognized lifecycle phase %r, reported as recorded' % phase
    return out


# --- Rendering ------------------------------------------------------------------------------------------------
def _short(invocation_id):
    return None if invocation_id is None else 'I'+invocation_id.rsplit(':', 1)[-1]


def _seconds(ms):
    return '' if ms is None else '%dms' % ms if ms < 1000 else '%.1fs' % (ms/1000)


def _clean(value, limit=160):
    text = ''.join(ch if ch.isprintable() else '?' for ch in str(value))  # Never terminal control sequences.
    return text if len(text) <= limit else text[:limit-3]+'...'


def _diagnostic(root, e):
    from .diagnostics import diagnostic_path
    return e['invocation_id'] is not None and diagnostic_path(root, e['invocation_id']).is_file()


def describe(root, e):
    """(column, label, detail, extra lines) for one trace event; None for events folded into another line."""
    p, t = e.get('payload') or {}, e['event_type']
    state = 'WORK' if t in _RUN_LEVEL or (t == 'run_interrupted' and not e['invocation_id']) else (e['state'] or '-')
    model = (e['provider'] if not e['model'] else '%s/%s' % (e['provider'], e['model'])) if e.get('provider') else ''
    if t == 'run_started':
        r = (p.get('provenance') or {}).get('runtime', {})
        g = (p.get('provenance') or {}).get('git', {})
        return state, 'Started', 'flow %s, switchyard %s, base %s' % (r.get('flow') or '-', r.get('switchyard_version') or '-',
                                                                       (g.get('delivery_base_sha') or '-')[:12]), []
    if t == 'state_entered':
        return state, 'Entered', 'visit %s%s' % (p.get('visit_number') or '-', ', waits for human input' if p.get('wait_for') else ''), []
    if t == 'routing_decided':
        return None
    if t == 'invocation_started':
        attempt = p.get('semantic_attempt')
        return state, 'Started', '%s  %s, %s, %s' % (model, e['agent'], _short(e['invocation_id']),
                                                     'attempt %s' % attempt if attempt else 'launch %s (not charged)' % p.get('invocation_in_visit')), []
    if t == 'invocation_finished':
        return state, 'Completed', '%s  %s' % (p.get('outcome'), _seconds(p.get('duration_ms'))), []
    if t == 'transition_decided':
        return state, 'Transition', '-> %s (%s)' % (p.get('to_state'), p.get('outcome') or p.get('trigger')), []
    if t == 'state_exited':
        return state, 'Exited', '', []
    if t == 'budget_exceeded':
        return state, 'Budget', '%s exhausted %s/%s -> %s' % (p.get('metric'), p.get('observed'), p.get('limit'), p.get('route_to') or 'stop'), []
    if t == 'human_input_requested':
        return state, 'Waiting', 'human input requested (%s)' % p.get('request_id'), []
    if t == 'human_input_received':
        return state, 'Answered', 'human input received%s' % (' -> %s' % p['target'] if p.get('target') else ''), []
    if t == 'run_resumed':
        return state, 'Resumed', '%s%s' % (p.get('resume_kind'), ' -> %s' % p['target'] if p.get('target') else ''), []
    if t == 'run_finished':
        return state, 'Finished', '%s  %s' % (p.get('terminal'), _seconds(p.get('duration_ms'))), []
    if t == 'run_interrupted':
        resume = 'resume available' if p.get('resume_safe') else 'resume not available'
        if not e['invocation_id']:
            return state, 'Interrupted', '%s, %s' % (p.get('reason') or 'unknown', resume), []
        reason = p.get('outcome_category') or p.get('reason') or 'unknown'
        how = ' (timeout)' if p.get('termination') == 'timeout' else ' (code %s)' % p['exit_code'] if p.get('exit_code') is not None else ''
        extra = ['Invocation %s | %s' % (_short(e['invocation_id']), 'Diagnostic available' if _diagnostic(root, e) else 'Diagnostic unavailable'),
                 ('WORK', 'Interrupted', '%s, %s' % (p.get('reason') or 'unknown', resume))]
        return state, 'Failed', reason+how, extra
    return state, t, '', []


def _time(e):
    try:
        return datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00')).astimezone().strftime('%H:%M:%S')
    except (AttributeError, TypeError, ValueError):
        return '--:--:--'


def lines(root, e):
    """Human lines for one event (possibly none)."""
    described = describe(root, e)
    if described is None:
        return []
    state, label, detail, extra = described
    stamp = _time(e)
    out = [('%s  %-19s %-12s%s' % (stamp, _clean(state, 19), label, _clean(detail))).rstrip()]
    for item in extra:
        out.append(('%s  %-19s %-12s%s' % (stamp, item[0], item[1], _clean(item[2])) if type(item) is tuple
                    else ' '*42+_clean(item)).rstrip())
    return out


def entry(root, e):
    """Self-contained JSON object for one trace event (logs --json items; watch --json lines)."""
    described = describe(root, e)
    summary = ('routed to %s/%s (agent %s, rung %s)' % (e['provider'], e['model'] or '-', e['agent'], e['rung'])
               if described is None else ' '.join(filter(None, (described[1], described[2].strip()))))
    out = dict(record='event', sequence=e['sequence'], event_id=e['event_id'], event_type=e['event_type'], work_id=e['work_id'],
               run_id=e['run_id'], state_visit_id=e['state_visit_id'], state=e['state'], invocation_id=e['invocation_id'],
               agent=e['agent'], provider=e['provider'], model=e['model'], timestamp=e['timestamp'],
               observed_by=e['observed_by'], summary=_clean(summary, 512))
    if e['event_type'] in ('invocation_finished', 'run_interrupted') and e['invocation_id']:
        from .diagnostics import diagnostic_id
        available = _diagnostic(root, e)
        out['diagnostic'] = dict(available=available, diagnostic_id=diagnostic_id(e['work_id'], e['invocation_id']) if available else None)
    return out


def header(events):
    runs = list(dict.fromkeys(e['run_id'] for e in events))
    work = events[0]['work_id'] if events else '-'
    return 'WORK  %s  RUN  %s' % (work, ', '.join(runs) or '-')


# --- logs -----------------------------------------------------------------------------------------------------
def _matches(e, run, state, invocation):
    from .diagnostics import _match
    return ((run is None or e['run_id'] == run) and (state is None or state in (e['state'], e['runtime_state']))
            and (invocation is None or (e['invocation_id'] is not None and _match(e['invocation_id'], invocation))))


def log_events(work_root, run=None, state=None, invocation=None, last=None):
    """(events, held_at): the consistent trace prefix, filtered; `held_at` names a missing sequence, if any."""
    cursor = TraceCursor(work_root)
    events = [e for e in cursor.poll() if _matches(e, run, state, invocation)]
    if last is not None:
        events = events[-last:] if last > 0 else []
    return events, cursor.waiting


def gap_warning(held_at):
    return ('trace incomplete: sequence %d is not (yet) published or readable although later events exist; '
            'later events are not shown, never skipped' % held_at)


def render_logs(work_root, events, held_at):
    if not events:
        text = ['No trace events available.']
    else:
        text = [header(events), '']
        for e in events:
            text += lines(work_root, e)
    if held_at is not None:
        text.append('warning: '+gap_warning(held_at))
    return '\n'.join(text)


# --- watch ----------------------------------------------------------------------------------------------------
def _status_line(s):
    parts = ['STATUS %s' % s['status']]
    if s.get('state') and s['active']:
        parts.append('state %s' % s['state'])
    if s.get('note'):
        parts.append(_clean(s['note'], 512))
    if s.get('next_action'):
        parts.append('next: '+s['next_action'])
    return ' | '.join(parts)


def watch(work_root, out, interval=1.0, last=10, json_mode=False, sleep=None, max_polls=None):
    """Follow the durable trace until stopped or a stable non-active status is observed. Read-only.

    Exits when the status has been non-active on two consecutive polls with no new events in between (a controller
    writes its next operation record moments after a transition), so transient phases do not end the watch. While a
    trace sequence is missing it waits up to GAP_PATIENCE further polls for the consistent prefix, then reports it.
    Returns 0 after a final status; KeyboardInterrupt propagates to the caller, which stops cleanly.
    """
    interval = min(MAX_INTERVAL, max(MIN_INTERVAL, float(interval)))
    sleep = sleep or time.sleep
    root = Path(work_root)
    cursor = TraceCursor(root)
    warned, noted, settled, held = set(), set(), None, 0

    def emit(record=None, text=None):
        if json_mode:
            out.write(json.dumps(record, sort_keys=True, ensure_ascii=True)+'\n')
        else:
            out.write(text+'\n')
        out.flush()

    def show(events):
        for e in events:
            if json_mode:
                emit(entry(root, e))
            else:
                for line in lines(root, e):
                    emit(text=line)

    def warn(cursor):
        if cursor.waiting is not None and cursor.waiting not in warned:
            warned.add(cursor.waiting)
            emit(dict(record='warning', sequence=cursor.waiting, warning=gap_warning(cursor.waiting)), 'warning: '+gap_warning(cursor.waiting))

    status = observed_status(root)
    history = cursor.poll()
    if json_mode:
        emit(dict(record='status', work_id=history[0]['work_id'] if history else None, **status, trace_events=len(history)))
    else:
        emit(text=header(history))
        emit(text=_status_line(status) + ' | %d trace event(s)' % len(history))
        if not history:
            emit(text='No trace events available.')
        emit(text='')
    show(history[-last:] if last > 0 else [])
    polls = 0
    while True:
        if status.get('note') and status['note'] not in noted and status['active']:
            noted.add(status['note'])
            emit(dict(record='warning', warning=status['note']), 'note: '+status['note'])
        if not status['active']:
            key = (status['status'], status['phase'], cursor.next)
            held = held + 1 if cursor.waiting is not None and settled == key else 0
            if settled == key and (cursor.waiting is None or held > GAP_PATIENCE):
                warn(cursor)
                emit(dict(record='status', final=True, **status), _status_line(status))
                return 0
            settled = key
        else:
            settled = None
        if max_polls is not None and polls >= max_polls:
            return 0
        sleep(interval)
        polls += 1
        new = cursor.poll()
        show(new)
        if cursor.waiting is not None and not new:
            warn(cursor)
        status = observed_status(root)
