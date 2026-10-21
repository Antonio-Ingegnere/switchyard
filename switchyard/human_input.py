"""Durable clarification records. Routing and invocation accounting remain runtime-owned."""
import json
import time

from .work_contract import WorkWorkspace, WorkContractError
from .workflow_bundle import ArtifactSpec

MAX_ANSWER_BYTES = 8192
CATALOG = {'request': ArtifactSpec('text', source='request'),
           'human_request': ArtifactSpec('json'), 'human_answer': ArtifactSpec('json')}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def store_for(workspace, create=False):
    path = workspace.root / 'human_input'
    if not create and not (path / 'manifest.jsonl').exists():
        return None
    store = WorkWorkspace.open(path, CATALOG)
    store.initialize_request(workspace.read_artifact('request'))
    return store


def request_targets(question):
    """Read versioned routing data without migrating immutable historical records."""
    version = question.get('schema_version')
    if type(version) is not int or version not in (1, 2):
        raise WorkContractError('unsupported human request schema version')
    targets = [question.get('resume_to')] if version == 1 else question.get('resume_targets')
    if (type(targets) is not list or not targets
            or any(type(t) is not str or not t for t in targets)
            or len(set(targets)) != len(targets)):
        raise WorkContractError('invalid human request continuation targets')
    if version == 2 and question.get('resume_to') != (targets[0] if len(targets) == 1 else None):
        raise WorkContractError('inconsistent human request continuation targets')
    return tuple(targets)


def select_target(question, selected=None):
    targets = request_targets(question)
    if selected is None and len(targets) == 1:
        return targets[0]
    if type(selected) is not str or selected not in targets:
        raise WorkContractError('select --resume-to from allowed targets: ' + ', '.join(targets))
    return selected


def answer_target(question, answer):
    version = answer.get('schema_version')
    if type(version) is not int or version not in (1, 2):
        raise WorkContractError('unsupported human answer schema version')
    if version == 1:
        if question['schema_version'] != 1:
            raise WorkContractError('legacy answer requires legacy single-target request')
        return select_target(question)
    if answer.get('selected_resume_to') is None:
        raise WorkContractError('human answer is missing selected_resume_to')
    return select_target(question, answer['selected_resume_to'])


def records(workspace):
    store = store_for(workspace)
    if store is None:
        return (), ()
    requests = tuple(json.loads(store.read_artifact('human_request', r.version)) for r in store.versions('human_request'))
    answers = tuple(json.loads(store.read_artifact('human_answer', r.version)) for r in store.versions('human_answer'))
    ids = [r['request_id'] for r in requests]
    answered = [a['request_id'] for a in answers]
    if (len(set(ids)) != len(ids) or len(set(answered)) != len(answered)
            or answered != ids[:len(answered)] or len(requests)-len(answers) not in (0, 1)):
        raise WorkContractError('inconsistent human input history')
    for question in requests:
        request_targets(question)
    for question, answer in zip(requests, answers):
        answer_target(question, answer)
        if (answer.get('work_id'), answer.get('run_id'), answer.get('sequence')) != (
                question.get('work_id'), question.get('run_id'), question.get('round')):
            raise WorkContractError('human answer provenance mismatch')
        validate_answer(answer.get('answer'))
    return requests, answers


def active_request(workspace):
    requests, answers = records(workspace)
    if not requests:
        raise WorkContractError('human input request is unavailable')
    question = requests[-1]
    answer = answers[-1] if answers and answers[-1]['request_id'] == question['request_id'] else None
    return question, answer


def validate_answer(text):
    valid = type(text) is str and bool(text.strip())
    try:
        size = len(text.encode('utf-8')) if valid else 0
    except UnicodeError:
        valid = False
    if not valid:
        raise WorkContractError('human answer must be nonempty valid UTF-8 text')
    if size > MAX_ANSWER_BYTES:
        raise WorkContractError('human answer exceeds 8192 UTF-8 bytes')


def write_answer(workspace, request_id, text, resume_to=None):
    validate_answer(text)
    question, answer = active_request(workspace)
    if question['request_id'] != request_id or answer is not None:
        raise WorkContractError('stale or already answered human input request')
    selected = select_target(question, resume_to)
    store = store_for(workspace)
    store.write_artifact('human_answer', _json(dict(schema_version=2, selected_resume_to=selected, request_id=request_id,
        sequence=question['round'], answer=text, answered_at_unix_ns=time.time_ns(),
        actor='human', work_id=question['work_id'], run_id=question['run_id'])))


def clarification_snapshots(workspace, flow, state):
    """Only the latest answered pair for this destination, never an unbounded transcript."""
    from .context_transport import ArtifactSnapshot
    requests, answers = records(workspace)
    matching = [(q, a) for q, a in zip(requests, answers) if q['flow'] == flow and answer_target(q, a) == state]
    if not matching:
        return ()
    question, answer = matching[-1]
    store = store_for(workspace)
    return tuple(ArtifactSnapshot(store.versions(name)[question['round']-1],
                                  store.read_artifact(name, question['round']))
                 for name in ('human_request', 'human_answer'))


class HumanInputRecorder:
    """Publish a question from the actual completed handoff before its wait event."""
    def __init__(self, workspace, compiled, delegate=None, before_wait=None):
        self.workspace, self.compiled, self.delegate = workspace, compiled, delegate
        self.before_wait = before_wait
        self.origin = None
        self.transition = None
        self.handoff_before = 0

    def record(self, event):
        if event.kind == 'invocation_started':
            self.handoff_before = len(self.workspace.handoffs())
        if event.kind == 'invocation_finished':
            self.origin = event
        if event.kind == 'transition_selected':
            self.transition = event
        if event.kind == 'human_input_requested':
            origin = self.origin
            handoffs = self.workspace.handoffs()
            if (origin is None or len(handoffs) != self.handoff_before + 1 or self.transition is None
                    or self.transition.target != event.state or self.transition.invocation_id != origin.invocation_id
                    or self.transition.outcome != origin.outcome):
                raise WorkContractError('human wait requires the originating invocation handoff')
            binding = self.compiled.plan[origin.state].binding
            handoff = handoffs[-1]
            if (handoff.flow, handoff.logical_state, handoff.rung_index, handoff.outcome) != (
                    binding.flow_name, binding.logical_state, binding.rung_index, origin.outcome):
                raise WorkContractError('human wait handoff origin mismatch')
            requests, answers = records(self.workspace)
            if len(requests) != len(answers):
                raise WorkContractError('human input already pending')
            if self.before_wait:
                self.before_wait()
            store = store_for(self.workspace, create=True)
            store.write_artifact('human_request', _json(dict(schema_version=2,
                request_id=event.state_visit_id, round=len(requests)+1,
                work_id=self.workspace.root.name, run_id=event.run_id, flow=binding.flow_name,
                wait_state=event.state, origin_state=binding.logical_state, origin_rung=binding.rung_index,
                origin_invocation_id=origin.invocation_id, origin_state_visit_id=origin.state_visit_id,
                origin_outcome=origin.outcome, handoff_sequence=handoff.sequence,
                summary=handoff.summary, blockers=list(handoff.blockers),
                question='\n'.join(handoff.blockers) if handoff.blockers else handoff.summary,
                created_at_unix_ns=event.occurred_at_unix_ns,
                resume_to=self.compiled.plan[event.state].declaration.single_resume_to,
                resume_targets=list(self.compiled.plan[event.state].declaration.resume_targets))))
        if self.delegate:
            self.delegate.record(event)
