#!/usr/bin/env python3
"""Manual REAL-provider dogfood. All target/work/evidence directories are retained."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from runtime.recording import JsonlRecorder
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.work_contract import WorkWorkspace, validate_microspec
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_execution import execute_v2_workflow

REQUEST = ("I want catalog lookups to optionally ignore capitalization.\n\n"
           "The current lookup behavior must continue working as it does now.\n"
           "Add the new behavior cleanly and cover it with tests.\n")
FIXTURE = REPO/'tests/fixtures/feature_dogfood_project'
BUNDLE = REPO/'examples/delivery/workflows-v2.yaml'


class DogfoodFailure(RuntimeError):
    pass


def inventory(target):
    result = {}
    for path in sorted(target.rglob('*.py')):
        if '__pycache__' in path.parts or any(part.startswith('.') for part in path.relative_to(target).parts):
            continue
        if path.is_symlink():
            raise DogfoodFailure('project source must not be a symlink')
        result[path.relative_to(target).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def changed(before, after):
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def baseline(target, argv, environment):
    # Verbosity changes reporting only; execution uses the canonical verifier argv.
    result = subprocess.run([*argv, '-v'], cwd=target, env=dict(environment),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    output = result.stdout.decode('utf-8', errors='replace')
    lines = output.splitlines()
    tests = [line for line in lines if line.startswith('test_')]
    expected = {
        'test_existing_exact_lookup': 'ok',
        'test_existing_case_sensitive_and_missing': 'ok',
        'test_optional_case_insensitive_exact_lookup': 'FAIL',
    }
    valid = (result.returncode == 1 and len(tests) == 3 and
             all(sum(line.startswith(name+' ') and line.endswith(' ... '+status)
                     for line in tests) == 1 for name, status in expected.items()) and
             'FAILED (failures=1)' in lines and 'AssertionError: None != 12' in lines)
    if not valid:
        raise DogfoodFailure('baseline did not show exactly the expected missing-feature failure: '+output[-2048:])
    return output


class EvidenceRecorder:
    def __init__(self, root, target, bundle):
        self.root, self.target, self.bundle = root, target, bundle
        self.recorder = JsonlRecorder(root/'events.jsonl')
        self.events, self.snapshots = [], []
        (root/'events.jsonl').touch(exist_ok=False)

    def record(self, event):
        self.recorder.record(event)
        self.events.append(event)
        if event.kind in ('invocation_started', 'invocation_finished'):
            workspace = WorkWorkspace.open(self.root/'work', self.bundle.artifacts)
            snapshot = dict(kind=event.kind, state=event.state, invocation_id=event.invocation_id,
                            files=inventory(self.target), request_unchanged=workspace.read_artifact('request') == REQUEST,
                            microspec_version=None, handoff_count=len(workspace.handoffs()))
            spec = workspace.latest_artifact('microspec')
            if spec is not None:
                validate_microspec(workspace.read_artifact('microspec'))
                snapshot['microspec_version'] = spec.version
            self.snapshots.append(snapshot)
            with (self.root/'observations.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(snapshot, sort_keys=True)+'\n')


def dogfood(config, *, root=None):
    """Copy fixture, validate baseline, execute canonical flow once; retain all evidence.

    config.working_directory is replaced with the new disposable target. The caller's
    project is never used as an agent working directory. No stage logic is supplied here.
    """
    root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix='switchyard-v2-005-'))
    root.mkdir(parents=True, exist_ok=True)
    target = root/'target'
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    config = replace(config, working_directory=str(target.resolve()))
    bundle = load_workflow_bundle(BUNDLE)
    command = bundle.profiles['command'].command
    before = inventory(target)
    (root/'baseline-files.json').write_text(json.dumps(before, indent=2)+'\n')
    recorder = EvidenceRecorder(root, target, bundle)
    report = dict(status='failed', flow='feature', terminal=None, target=str(target),
                  work=str(root/'work'), events=str(root/'events.jsonl'),
                  verification_command=list(command.argv), provider_invocations=[], changed_files=[])
    print('Retained dogfood evidence:', root, flush=True)
    try:
        output = baseline(target, command.argv, config.environment)
        (root/'baseline-verification.txt').write_text(output, encoding='utf-8')
        report['baseline'] = 'two existing tests pass; one expected feature failure'
        execution = execute_v2_workflow(bundle, 'feature', REQUEST, root/'work', config, recorder=recorder)
        workspace, compiled = execution.workspace, execution.compiled
        report['terminal'] = execution.result.terminal_state
        bindings = {key: plan.binding for key, plan in compiled.plan.items()}
        report['state_sequence'] = [compiled.plan[e.state].logical_state for e in recorder.events if e.kind == 'state_entered']
        report['provider_invocations'] = [dict(
            logical_state=bindings[e.state].logical_state, profile=bindings[e.state].profile_name,
            provider=bindings[e.state].provider, model_selector=bindings[e.state].model,
            resolved_model=config.model_aliases.get(bindings[e.state].provider, {}).get(bindings[e.state].model, bindings[e.state].model),
            effort=bindings[e.state].effort, rung=bindings[e.state].rung_index)
            for e in recorder.events if e.kind == 'invocation_started' and bindings[e.state].provider != 'command']
        report['escalation_occurred'] = any(item['rung'] > 0 for item in report['provider_invocations'])
        report['repair_occurred'] = 'REPAIR_ONCE' in report['state_sequence']
        report['verification_outcomes'] = [e.outcome for e in recorder.events if e.kind == 'invocation_finished'
                                           and bindings[e.state].provider == 'command']
        report['changed_files'] = changed(before, inventory(target))
        report['artifacts'] = {name: [r.version for r in workspace.versions(name)] for name in bundle.artifacts
                               if workspace.latest_artifact(name) is not None}
        report['handoff_count'] = len(workspace.handoffs())
        starts = {s['invocation_id']:s for s in recorder.snapshots if s['kind'] == 'invocation_started'}
        implement = [s for s in recorder.snapshots if s['kind'] == 'invocation_finished'
                     and bindings[s['state']].logical_state == 'IMPLEMENT']
        if len(implement) != 1:
            raise DogfoodFailure('expected one completed IMPLEMENT invocation')
        end = implement[0]; start = starts[end['invocation_id']]
        if not start['microspec_version'] or not start['handoff_count'] or not start['request_unchanged']:
            raise DogfoodFailure('SPECIFY did not commit the required durable contract before IMPLEMENT')
        if not changed(start['files'], end['files']):
            raise DogfoodFailure('IMPLEMENT made no intended source/test change')
        # Agents may add tests; they may not erase the independent acceptance oracle.
        if inventory(target).get('tests/test_catalog.py') != before['tests/test_catalog.py']:
            raise DogfoodFailure('independent acceptance tests were modified')
        if workspace.read_artifact('request') != REQUEST:
            raise DogfoodFailure('request changed')
        if report['terminal'] != 'READY_FOR_MANUAL_QA' or report['verification_outcomes'][-1:] != ['pass']:
            raise DogfoodFailure('canonical FEATURE did not reach verified READY_FOR_MANUAL_QA')
        if workspace.latest_artifact('verifier_evidence') is None:
            raise DogfoodFailure('verifier evidence missing')
        report['status'] = 'passed'
    except Exception as exc:
        report['errors'] = []
        current = exc
        while current is not None:
            report['errors'].append(dict(type=type(current).__name__, message=str(current)[:2048]))
            diagnostics = getattr(current, 'diagnostics', None)
            if diagnostics is not None:
                report['errors'][-1]['diagnostics'] = diagnostics.utf8.decode('utf-8')[:4096]
            current = current.__cause__
        report['invocations_started'] = sum(e.kind == 'invocation_started' for e in recorder.events)
        report['changed_files'] = changed(before, inventory(target))
        raise DogfoodFailure('Dogfood failed; inspect '+str(root/'report.json')) from exc
    finally:
        if (root/'work/manifest.jsonl').exists():
            try:
                workspace = WorkWorkspace.open(root/'work', bundle.artifacts)
                report['artifacts'] = {name: [r.version for r in workspace.versions(name)]
                                       for name in bundle.artifacts if workspace.latest_artifact(name) is not None}
                report['handoff_count'] = len(workspace.handoffs())
            except Exception as evidence_error:
                report['evidence_inspection_error'] = str(evidence_error)[:2048]
        (root/'changed-files.json').write_text(json.dumps(report['changed_files'], indent=2)+'\n')
        (root/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude', default='claude')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--model-aliases', type=Path, help='Local JSON provider/selector mapping; never committed')
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args()
    aliases = {} if args.model_aliases is None else json.loads(args.model_aliases.read_text())
    config = ProviderRuntimeConfig(str(REPO), args.claude, args.codex,
                                   model_aliases=aliases, timeout_seconds=args.timeout)
    try:
        report = dogfood(config)
    except DogfoodFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
