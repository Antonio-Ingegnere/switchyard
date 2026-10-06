"""Offline V2 lifecycle example: fake CLI delivery, real verifier/Git, process restart.

Run: python -m examples.delivery.offline_lifecycle --root /tmp/new-qa-example
The fresh external root is retained. No real provider executable is used.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from runtime import RunResult
from runtime.recording import JsonlRecorder
from switchyard.observability import read_events, summarize_runs
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.qa_lifecycle import create_qa_candidate, accept_v2_work, reopen_v2_work
from switchyard.work_contract import WorkWorkspace
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_execution import execute_v2_workflow

REPO = Path(__file__).resolve().parents[2]
FIX = '''"""Exact catalog lookup with optional case-insensitive comparison."""
ITEMS = {"Notebook": 12, "Pencil": 3, "Desk Lamp": 25}
def lookup(name, *, ignore_case=False):
    if ignore_case:
        return next((price for key, price in ITEMS.items() if key.casefold() == name.casefold()), None)
    return ITEMS.get(name)
'''
REQUEST = 'Add optional case-insensitive exact catalog lookup; preserve existing behavior.'
FEEDBACK = 'Please review the naming before acceptance.'


def git(target, *args):
    return subprocess.run(['git', *args], cwd=target, check=True, capture_output=True).stdout.decode().strip()


def lifecycle_from_disk(root, operation):
    """Consumer reconstruction recipe, not a new framework resume API."""
    from unittest.mock import patch
    # Both construction and invocation would fail if lifecycle ever used a model.
    with patch('switchyard.provider_stages.ProviderStageExecutorResolver.__call__',
               side_effect=AssertionError('provider resolution during QA')), \
         patch('switchyard.provider_stages.execute', side_effect=AssertionError('provider execution during QA')):
        workspace = WorkWorkspace.open(root/'work')
        events = tuple(read_events(root/'events.jsonl'))
        summaries = summarize_runs(events)  # Validate canonical persisted history.
        assert len(summaries) == 1 and events[-1].kind == 'run_finished'
        result = RunResult(events[-1].state, events)
        assert result.terminal_state == 'READY_FOR_MANUAL_QA'
        candidate = create_qa_candidate(root/'target', workspace, result, 'feature', root/'qa')
        # Reopen the candidate through its durable location, not the Python object.
        identity = candidate.identity
        del candidate, result, workspace, events
        qa = WorkWorkspace.open(root/'qa')
        assert qa.latest_artifact('qa_candidate').sha256 == identity
        decision = (accept_v2_work(root/'target', root/'qa') if operation == 'accept'
                    else reopen_v2_work(root/'target', root/'qa', FEEDBACK))
        return {**asdict(decision), 'candidate': json.loads(qa.read_artifact('qa_candidate')),
                'feedback': qa.read_artifact('qa_feedback') if operation == 'reopen' else None,
                'lifecycle_pid': os.getpid(), 'provider_calls_during_qa': 0}


def dogfood(root, operation):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = root/'target'
    shutil.copytree(REPO/'tests/fixtures/feature_dogfood_project', target,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (target/'.gitignore').write_text('__pycache__/\n*.pyc\n')
    git(target, 'init', '-b', 'main')
    git(target, 'config', 'user.name', 'Offline Lifecycle')
    git(target, 'config', 'user.email', 'qa@example.invalid')
    git(target, 'config', 'commit.gpgsign', 'false')
    git(target, 'add', '--', '.gitignore', 'catalog.py', 'tests/test_catalog.py')
    git(target, 'commit', '-m', 'baseline')
    base = git(target, 'rev-parse', 'HEAD')
    tests_digest = hashlib.sha256((target/'tests/test_catalog.py').read_bytes()).hexdigest()
    fake_bin = root/'bin'; fake_bin.mkdir()
    script = (REPO/'tests/fixtures/provider_cli.py').read_text()
    script = script.replace("outcome=plan.get", "if state == 'IMPLEMENT':\n    Path('catalog.py').write_text("+repr(FIX)+")\noutcome=plan.get")
    # Retain binding metadata only, never full prompts (even for these fake processes).
    script = script.replace("'input':text,", '')
    for provider in ('claude', 'codex'):
        path = fake_bin/provider
        path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
    spec = json.dumps(dict(id='OFFLINE-FEATURE', goal=REQUEST, scope=['catalog.py'],
                          constraints=['Preserve exact lookup'], acceptance=['Three tests pass'],
                          verification=['python3 -m unittest discover -s tests'], non_goals=[]))
    plan = {'artifacts': {'SPECIFY': [{'name':'microspec', 'content':spec}],
                         'IMPLEMENT': [{'name':'implementation_handoff', 'content':'Implemented optional lookup.'}]}}
    (root/'plan.json').write_text(json.dumps(plan))
    env = {**os.environ, 'PATH':str(Path(sys.executable).parent)+os.pathsep+os.defpath,
           'FAKE_PLAN':str(root/'plan.json'), 'FAKE_LOG':str(root/'calls.jsonl')}
    config = ProviderRuntimeConfig(str(target), str(fake_bin/'claude'), str(fake_bin/'codex'),
                                    environment=env, timeout_seconds=10,
                                    model_aliases={'codex':{'codex':'offline-model'}})
    bundle = load_workflow_bundle(REPO/'examples/delivery/workflows-v2.yaml')
    execution = execute_v2_workflow(bundle, 'feature', REQUEST, root/'work', config,
                                    recorder=JsonlRecorder(root/'events.jsonl'))
    sequence = [execution.compiled.plan[e.state].logical_state for e in execution.result.events
                if e.kind == 'state_entered']
    assert sequence == ['SPECIFY', 'IMPLEMENT', 'VERIFY', 'READY_FOR_MANUAL_QA']
    assert git(target, 'rev-parse', 'HEAD') == base  # Delivery never commits.
    assert 'OK' in execution.workspace.read_artifact('verifier_evidence')
    assert len(execution.workspace.handoffs()) == 2
    artifacts = {name: [r.version for r in execution.workspace.versions(name)]
                 for name in ('request', 'microspec', 'implementation_handoff', 'verifier_evidence')}
    delivered = (target/'catalog.py').read_bytes()
    calls = (root/'calls.jsonl').read_bytes()
    assert len(calls.splitlines()) == 2
    del execution, config, bundle  # Child receives paths and the human operation only.
    child = subprocess.run([sys.executable, '-m', 'examples.delivery.offline_lifecycle',
                            '--root', str(root), '--operation', operation, '--lifecycle-only'],
                           cwd=REPO, check=True, capture_output=True, text=True)
    decision = json.loads(child.stdout)
    assert decision['lifecycle_pid'] != os.getpid()
    assert (root/'calls.jsonl').read_bytes() == calls
    assert decision['candidate']['files'] == {'catalog.py': {
        'kind':'file', 'sha256':hashlib.sha256(delivered).hexdigest(), 'mode':'100644'}}
    assert (target/'catalog.py').read_bytes() == delivered
    assert hashlib.sha256((target/'tests/test_catalog.py').read_bytes()).hexdigest() == tests_digest
    head = git(target, 'rev-parse', 'HEAD')
    if operation == 'accept':
        assert decision['terminal'] == 'ACCEPTED' and decision['committed_head'] == head
        assert git(target, 'rev-parse', 'HEAD^') == base
        assert git(target, 'rev-list', '--count', base+'..HEAD') == '1'
        assert git(target, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD') == 'catalog.py'
        blob = subprocess.run(['git', 'show', 'HEAD:catalog.py'], cwd=target, capture_output=True, check=True).stdout
        assert blob == delivered and git(target, 'status', '--porcelain') == ''
    else:
        assert decision['terminal'] == 'REOPENED' and head == base
        assert decision['feedback'] == FEEDBACK
        assert git(target, 'diff', '--cached', '--name-only') == ''
        assert git(target, 'diff', '--name-only') == 'catalog.py'
    report = {**decision, 'state_sequence':sequence, 'base_head':base, 'final_head':head,
              'delivery_pid':os.getpid(), 'artifacts':artifacts, 'handoffs':2,
              'verifier':'python3 -m unittest discover -s tests', 'verifier_result':'3 passed',
              'provider_processes':'two fake Claude processes; zero live providers',
              'acceptance_tests_unchanged':True, 'changed_paths':['catalog.py']}
    (root/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--operation', choices=('accept', 'reopen'), default='accept')
    parser.add_argument('--lifecycle-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = lifecycle_from_disk(args.root, args.operation) if args.lifecycle_only else dogfood(args.root, args.operation)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
