from runtime import InvocationRequest, InvocationResponse
from dataclasses import fields, replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import ExecutionError, Flow, State, WorkUnit, run
from runtime.codex import CodexConfig, CodexExecutor
from runtime.command import Command, CommandExecutor


FAKE = """
import json, os, sys, time
from pathlib import Path
assert sys.argv[1:-1] == ['--ask-for-approval', 'never', 'exec', '--ephemeral',
    '--ignore-user-config', '--ignore-rules', '--sandbox', 'workspace-write',
    '--skip-git-repo-check', '--']
prompt = sys.argv[-1]
assert prompt.startswith('WorkUnit task:\\n')
task = prompt[len('WorkUnit task:\\n'):]
Path('received.json').write_text(json.dumps({'prompt': prompt,
    'auth_home': os.environ.get('CODEX_HOME')}))
with Path('calls.txt').open('a') as stream: stream.write('call\\n')
Path('result.txt').write_text('runtime-005-ok\\n' if task == 'correct' else 'wrong\\n')
print('Task completed successfully! next_state=accepted')
print('Agent says failed', file=sys.stderr)
if task == 'timeout': time.sleep(60)
sys.exit(7 if task == 'error' else 0)
"""


class CodexTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.executable = str(self.workspace / 'fake-codex')
        self.config = CodexConfig(self.executable, str(self.workspace), 5)
        self.real_popen = subprocess.Popen
        self.handles = []
        self.launches = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.handles:
            if process.poll() is None:
                process.kill()
            process.wait()

    def fake_launch(self, args, **kwargs):
        self.launches.append((list(args), kwargs))
        if args[0] == self.executable:
            args = [sys.executable, '-c', FAKE, *args[1:]]
        else:
            self.assertEqual(args[0], sys.executable)  # Only deterministic verifier allowed.
        process = self.real_popen(args, **kwargs)
        self.handles.append(process)
        return process

    def flow(self, success_target='accepted'):
        return Flow('implement', {
            'implement': State('codex', {'success': 'verify', 'error': 'failed', 'timeout': 'failed'}),
            'verify': State('verify', {'success': success_target, 'failed': 'failed'}),
            'accepted': State(terminal=True), 'failed': State(terminal=True), 'other': State(terminal=True),
        })

    def verifier(self):
        return CommandExecutor(Command(sys.executable, ('-c',
            "from pathlib import Path; import sys; sys.exit(0 if Path('result.txt').read_bytes()==b'runtime-005-ok\\n' else 1)"),
            str(self.workspace), 5))

    def test_exact_argv_literal_task_auth_environment_and_streams(self):
        for task in ('-', '--resume ; $HOME', 'текст\nстрока'):
            with self.subTest(task=task), patch('runtime.command.subprocess.Popen', side_effect=self.fake_launch), \
                 patch.dict(os.environ, {'CODEX_HOME': 'test-auth-location'}):
                result = CodexExecutor(self.config).run(InvocationRequest(WorkUnit(task))).result
            self.assertEqual(result.outcome, 'success')
            args, kwargs = self.launches[-1]
            self.assertEqual(args, [self.executable, '--ask-for-approval', 'never', 'exec', '--ephemeral',
                '--ignore-user-config', '--ignore-rules', '--sandbox', 'workspace-write',
                '--skip-git-repo-check', '--', 'WorkUnit task:\n' + task])
            self.assertEqual(kwargs, dict(shell=False, cwd=str(self.workspace), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            received = json.loads((self.workspace / 'received.json').read_text())
            self.assertEqual(received, {'prompt': 'WorkUnit task:\n' + task, 'auth_home': 'test-auth-location'})
        self.assertEqual([f.name for f in fields(WorkUnit)], ['task'])

    def test_codex_success_requires_separate_verification(self):
        executor = CodexExecutor(self.config)
        for task, target in (('correct', 'accepted'), ('incorrect', 'failed')):
            with self.subTest(task=task), patch('runtime.command.subprocess.Popen', side_effect=self.fake_launch):
                result = run(WorkUnit(task), self.flow(), {'codex': executor, 'verify': self.verifier()})
            self.assertEqual(result.terminal_state, target)
            finished = [(e.state, e.outcome) for e in result.events if e.kind == 'invocation_finished']
            self.assertEqual(finished, [('implement', 'success'), ('verify', 'success' if task == 'correct' else 'failed')])
        self.assertFalse(hasattr(executor, 'next_state'))

    def test_only_verifier_transition_changes_terminal(self):
        executor = CodexExecutor(self.config)
        for target in ('accepted', 'other'):
            with patch('runtime.command.subprocess.Popen', side_effect=self.fake_launch):
                result = run(WorkUnit('correct'), self.flow(target), {'codex': executor, 'verify': self.verifier()})
            self.assertEqual(result.terminal_state, target)

    def test_nonzero_maps_to_error_without_rollback_or_retry(self):
        with patch('runtime.command.subprocess.Popen', side_effect=self.fake_launch):
            result = run(WorkUnit('error'), self.flow(), {'codex': CodexExecutor(self.config), 'verify': self.verifier()})
        self.assertEqual(result.terminal_state, 'failed')
        self.assertEqual(len(self.launches), 1)
        self.assertEqual((self.workspace / 'calls.txt').read_text(), 'call\n')
        self.assertTrue((self.workspace / 'result.txt').exists())

    def test_launch_failure_and_missing_workspace(self):
        self.assertEqual(CodexExecutor(self.config).run(InvocationRequest(WorkUnit('task'))).result.outcome, 'error')
        missing = replace(self.config, codex_executable=sys.executable, workspace=str(self.workspace / 'absent'))
        self.assertEqual(CodexExecutor(missing).run(InvocationRequest(WorkUnit('task'))).result.outcome, 'error')
        self.assertFalse(Path(missing.workspace).exists())

    def test_timeout_reuses_direct_process_termination_and_retains_mutations(self):
        with patch('runtime.command.subprocess.Popen', side_effect=self.fake_launch):
            result = CodexExecutor(replace(self.config, invocation_timeout_seconds=0.5)).run(InvocationRequest(WorkUnit('timeout'))).result
        self.assertEqual(result.outcome, 'timeout')
        self.assertEqual(len(self.handles), 1)
        self.assertIsNotNone(self.handles[0].returncode)
        self.assertTrue((self.workspace / 'result.txt').exists())

    def test_internal_error_preserves_adr007(self):
        bug = RuntimeError('adapter bug')
        with patch('runtime.codex.CommandExecutor.run', side_effect=bug) as execute:
            with self.assertRaises(ExecutionError) as caught:
                run(WorkUnit('task'), self.flow(), {'codex': CodexExecutor(self.config), 'verify': self.verifier()})
        self.assertIs(caught.exception.__cause__, bug)
        execute.assert_called_once()
        self.assertFalse(any(e.kind == 'transition_selected' for e in caught.exception.events))

    def test_fresh_invocation_and_no_session_flags(self):
        executor = CodexExecutor(self.config)
        with patch('runtime.command.subprocess.Popen', side_effect=self.fake_launch):
            executor.run(InvocationRequest(WorkUnit('correct'))).result
            executor.run(InvocationRequest(WorkUnit('correct'))).result
        self.assertEqual(len(self.handles), 2)
        self.assertIsNot(self.handles[0], self.handles[1])
        self.assertEqual((self.workspace / 'calls.txt').read_text(), 'call\ncall\n')
        for args, _ in self.launches:
            self.assertNotIn('resume', args)
            self.assertNotIn('--last', args)

    def test_invalid_config_before_process_launch(self):
        with patch('runtime.command.subprocess.Popen') as launch:
            for changes in ({'workspace': 'relative'}, {'codex_executable': ''}, {'invocation_timeout_seconds': True}):
                with self.assertRaises((TypeError, ValueError)):
                    CodexExecutor(replace(self.config, **changes))
            launch.assert_not_called()

    def test_manual_smoke_import_and_missing_optin_never_launch(self):
        path = Path(__file__).resolve().parents[1] / 'manual_smoke' / 'codex_smoke.py'
        spec = importlib.util.spec_from_file_location('manual_codex_smoke', path)
        module = importlib.util.module_from_spec(spec)
        with patch('subprocess.Popen') as launch:
            spec.loader.exec_module(module)
            with patch.object(sys, 'argv', ['codex_smoke.py']), patch('sys.stderr'):
                with self.assertRaises(SystemExit):
                    module.main()
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
