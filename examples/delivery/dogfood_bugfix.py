#!/usr/bin/env python3
"""Manual REAL-provider V2-006 proofs. One selected part per call; evidence is retained."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from examples.delivery.dogfood_feature import DogfoodFailure, inventory, changed
from runtime import InvocationRequest, WorkUnit
from runtime.recording import JsonlRecorder
from switchyard.context_transport import workspace_resolver
from switchyard.provider_stages import ProviderRuntimeConfig, ProviderStageExecutorResolver
from switchyard.work_contract import WorkWorkspace, validate_microspec
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow
from switchyard.workflow_execution import execute_v2_workflow

FIXTURE = REPO/'tests/fixtures/bugfix_dogfood_project'
REQUEST = ('There is something wrong with task ordering.\n\n'
           'If I add several tasks with the same priority, process one of them, and then add\n'
           'another task, the queue sometimes returns them in a different order from the\n'
           'order they were added. Tasks with equal priority should stay FIFO.\n\n'
           'Please reproduce it, find the cause and fix it without breaking priority ordering.\n')
ESCALATION = REPO/'examples/escalation'


def baseline(target, command, environment):
    result = subprocess.run([*command, '-v'], cwd=target, env=dict(environment),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    text = result.stdout.decode('utf-8', errors='replace')
    expected = {'test_priority_ordering':'ok', 'test_equal_priority_fifo':'ok',
                'test_fifo_after_remove_then_insert':'FAIL'}
    tests = [line for line in text.splitlines() if line.startswith('test_')]
    valid = (result.returncode == 1 and len(tests) == 3 and 'FAILED (failures=1)' in text
             and all(sum(line.startswith(name+' ') and line.endswith(' ... '+status)
                         for line in tests) == 1 for name, status in expected.items())
             and "['a-new', 'z-older'] != ['z-older', 'a-new']" in text)
    return text, valid


class EvidenceRecorder:
    """Observe admission using the same context builder, never invoke a stage here.

    The preflight graph is identical to the runner graph. Observations are snapshots
    immediately before the runtime calls the wrapper; fake-process tests independently
    compare these against the actual rendered input. No prompt/content is logged.
    """
    def __init__(self, root, target, compiled, request, config):
        self.root, self.target, self.compiled = root, target, compiled
        self.request, self.config = request, config
        self.events, self.observations = [], []
        self.recorder = JsonlRecorder(root/'events.jsonl')
        (root/'events.jsonl').touch(exist_ok=False)

    def record(self, event):
        self.recorder.record(event)
        self.events.append(event)
        if event.kind not in ('invocation_started', 'invocation_finished'):
            return
        plan = self.compiled.plan[event.state]
        binding = plan.binding
        item = dict(kind=event.kind, invocation_id=event.invocation_id, binding=asdict(binding),
                    resolved_model=self.config.model_aliases.get(binding.provider, {}).get(binding.model, binding.model),
                    files=inventory(self.target), outcome=event.outcome)
        workspace = self.compiled.executors[event.state].workspace
        item['artifacts_committed'] = {name:[r.version for r in workspace.versions(name)] for name in workspace.artifacts
                                       if workspace.latest_artifact(name) is not None}
        item['handoff_count'] = len(workspace.handoffs())
        item['request_unchanged'] = workspace.read_artifact('request') == self.request
        if event.kind == 'invocation_started':
            context = self.compiled.executors[event.state]._context(InvocationRequest(WorkUnit(self.request)))
            item['inputs'] = [dict(name=a.record.name, version=a.record.version, sha256=a.record.sha256) for a in context.artifacts]
            previous = context.previous_handoff
            item['previous_handoff'] = None if previous is None else dict(sequence=previous.sequence, flow=previous.flow,
                logical_state=previous.logical_state, rung_index=previous.rung_index, profile=previous.profile_name)
            item['allowed_outcomes'] = context.output_contract.allowed_outcomes
        self.observations.append(item)
        with (self.root/'observations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item, sort_keys=True)+'\n')


def dogfood(config, part, *, root=None):
    if part not in ('a', 'b'):
        raise ValueError('part must be a or b')
    root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix='switchyard-v2-006-'+part+'-'))
    root.mkdir(parents=True, exist_ok=True)
    target = root/'target'
    if part == 'a':
        shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    else:
        target.mkdir()
    config = replace(config, working_directory=str(target.resolve()))
    flow = 'bugfix' if part == 'a' else 'escalation'
    bundle = load_workflow_bundle(REPO/'examples/delivery/workflows-v2.yaml' if part == 'a' else ESCALATION/'workflows-v2.yaml')
    request = REQUEST if part == 'a' else (ESCALATION/'request.md').read_text()
    before = inventory(target)
    (root/'baseline-files.json').write_text(json.dumps(before, indent=2)+'\n')
    report = dict(status='failed', part=part, flow=flow, terminal=None, target=str(target), work=str(root/'work'),
                  events=str(root/'events.jsonl'), changed_files=[], provider_invocations=[])
    recorder = None
    print('Retained dogfood evidence:', root, flush=True)
    try:
        if part == 'a':
            command = bundle.profiles['command'].command.argv
            report['verification_command'] = list(command)
            text, valid = baseline(target, command, config.environment)
            (root/'baseline-verification.txt').write_text(text)
            if not valid:
                raise DogfoodFailure('baseline is not exactly the expected single FIFO failure')
        workspace = WorkWorkspace.open(root/'work', bundle.artifacts)
        workspace.initialize_request(request)
        if part == 'b':
            workspace.write_artifact('reproduction_evidence', (ESCALATION/'reproduction_evidence.md').read_text())
        provider = ProviderStageExecutorResolver(bundle.profiles, config)
        compiled = compile_workflow(bundle, flow, workspace_resolver(bundle, flow, workspace, provider))
        if part == 'b':
            rungs = [p for p in compiled.plan.values() if p.binding]
            if ([p.executor_allowed_outcomes for p in rungs] != [('inconclusive',),('diagnosis_disproved',),('ready',)]
                    or any(p.binding.capabilities != () for p in rungs)):
                raise DogfoodFailure('escalation contracts differ from the declared proof')
        recorder = EvidenceRecorder(root, target, compiled, request, config)
        execution = execute_v2_workflow(bundle, flow, request, root/'work', config, recorder=recorder)
        report['terminal'] = execution.result.terminal_state
        starts = [o for o in recorder.observations if o['kind']=='invocation_started']
        finishes = [o for o in recorder.observations if o['kind']=='invocation_finished']
        if workspace.read_artifact('request') != request:
            raise DogfoodFailure('immutable request changed')
        if part == 'a':
            for name in ('bug_spec','reproduction_evidence','diagnosis','fix_microspec','verifier_evidence'):
                if workspace.latest_artifact(name) is None:
                    raise DogfoodFailure('required durable artifact missing: '+name)
                if name in ('bug_spec','fix_microspec'):
                    validate_microspec(workspace.read_artifact(name))
            if before['tests/test_task_queue.py'] != inventory(target).get('tests/test_task_queue.py'):
                raise DogfoodFailure('independent acceptance tests changed')
            fixed = [o for o in finishes if o['binding']['logical_state'] in ('FIX','REFIX')]
            if not any(o['files'].get('task_queue.py') != before['task_queue.py'] for o in fixed):
                raise DogfoodFailure('coding stage did not change source')
            report['acceptance_tests_unchanged'] = True
            report['verification_outcomes'] = [o['outcome'] for o in finishes if o['binding']['provider']=='command']
            if report['terminal'] != 'READY_FOR_MANUAL_QA' or report['verification_outcomes'][-1:] != ['pass']:
                raise DogfoodFailure('canonical BUGFIX did not reach verified READY_FOR_MANUAL_QA')
        else:
            if [o['binding']['provider'] for o in starts] != ['claude','claude','codex']:
                raise DogfoodFailure('incorrect provider sequence')
            handoffs = workspace.handoffs()
            if len(handoffs) != 3 or [h.rung_index for h in handoffs] != [0,1,2]:
                raise DogfoodFailure('each rung must commit one handoff')
            if (starts[0]['previous_handoff'] is not None or starts[2]['previous_handoff'] is not None
                    or starts[1]['previous_handoff']['sequence'] != handoffs[0].sequence):
                raise DogfoodFailure('reasoning lineage differs from continue/fresh contract')
            if [a['name'] for a in starts[2]['inputs']] != ['request','reproduction_evidence']:
                raise DogfoodFailure('fresh Codex did not receive declared facts')
            if report['terminal'] != 'ASSESSED':
                raise DogfoodFailure('assessment did not complete')
        report['status'] = 'passed'
    except Exception as exc:
        report['errors'] = []
        current = exc
        while current is not None:
            item = dict(type=type(current).__name__, message=str(current)[:2048])
            diagnostic = getattr(current, 'diagnostics', None)
            if diagnostic is not None:
                item['diagnostics'] = diagnostic.utf8.decode('utf-8')[:4096]
            report['errors'].append(item)
            current = current.__cause__
        raise DogfoodFailure('Dogfood failed; inspect '+str(root/'report.json')) from exc
    finally:
        report['changed_files'] = changed(before, inventory(target))
        if recorder:
            report['state_sequence'] = [recorder.compiled.plan[e.state].logical_state for e in recorder.events if e.kind=='state_entered']
            report['provider_invocations'] = [o for o in recorder.observations if o['kind']=='invocation_started' and o['binding']['provider']!='command']
            report['event_kinds'] = [e.kind for e in recorder.events]
        if (root/'work/manifest.jsonl').exists():
            workspace = WorkWorkspace.open(root/'work', bundle.artifacts)
            report['artifacts'] = {name:[r.version for r in workspace.versions(name)] for name in bundle.artifacts if workspace.latest_artifact(name) is not None}
            report['handoff_count'] = len(workspace.handoffs())
        (root/'changed-files.json').write_text(json.dumps(report['changed_files'], indent=2)+'\n')
        (root/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--part', choices=['a','b'], required=True)
    parser.add_argument('--claude', default='claude')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--model-aliases', type=Path, required=True, help='Existing local JSON mapping; never committed')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args()
    config = ProviderRuntimeConfig(str(REPO), args.claude, args.codex,
                                   model_aliases=json.loads(args.model_aliases.read_text()), timeout_seconds=args.timeout)
    try:
        report = dogfood(config, args.part, root=args.root)
    except DogfoodFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
