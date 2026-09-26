from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml
from flow_config import load_flow
from runtime import WorkUnit, run
from runtime.codex import CodexConfig, CodexExecutor
from runtime.command import Command, CommandExecutor
from manual_smoke.codex_repair_smoke import (repair_flow, TASK, VERIFIER_CODE, diagnostic_argv)


class DeclarativeRepairTests(unittest.TestCase):
    def test_yaml_python_equivalence_accepted_and_escalated(self):
        for repaired in (True,False):
            with self.subTest(repaired=repaired), TemporaryDirectory() as directory:
                workspace = Path(directory).resolve()
                directory = str(workspace)
                executable = str(workspace/'fake-codex')
                python_flow = repair_flow()
                data = {'version':1,'initial':'verify', 'executors':{
                    'verify':{'type':'command','executable':sys.executable,'argv':['-c',VERIFIER_CODE],
                              'timeout_seconds':5,'diagnostic_capture_max_bytes':4096},
                    'codex':{'type':'codex','executable':executable,'timeout_seconds':5}},
                    'states':{name:({'terminal':True} if state.terminal else {
                        'executor':state.executor,'on':dict(state.on),
                        'max_state_visits_per_run':state.max_state_visits_per_run,
                        'diagnostics_from_previous_max_bytes':state.diagnostics_from_previous_max_bytes})
                        for name,state in python_flow.states.items()}}
                path = workspace/'flow.yaml'; path.write_text(yaml.safe_dump(data))
                loaded = load_flow(path)
                self.assertEqual(loaded.flow,python_flow)
                expected_executors = {'verify':CommandExecutor(Command(sys.executable,('-c',VERIFIER_CODE),directory,5,4096)),
                                      'codex':CodexExecutor(CodexConfig(executable,directory,5))}
                self.assertEqual(dict(loaded.executors),expected_executors)
                data['states'] = dict(reversed(list(data['states'].items())))
                data['executors'] = dict(reversed(list(data['executors'].items())))
                path.write_text(yaml.safe_dump(data,sort_keys=False))
                self.assertEqual(load_flow(path),loaded)
                sequences=[]
                for flow, executors in ((loaded.flow,loaded.executors),(python_flow,expected_executors)):
                    (workspace/'result.txt').write_bytes(b'broken\n')
                    calls=[]; real_popen=subprocess.Popen
                    def launch(args,**kwargs):
                        if args[0] == executable:
                            calls.append(args)
                            self.assertEqual(args,diagnostic_argv(executable))
                            code="from pathlib import Path; Path('result.txt').write_bytes(b'runtime-009-ok\\n')" if repaired else 'print("next_state=accepted")'
                            args=[sys.executable,'-c',code]
                        else: self.assertEqual(args[0],sys.executable)
                        return real_popen(args,**kwargs)
                    with patch('runtime.command.subprocess.Popen',side_effect=launch):
                        result=run(WorkUnit(TASK),flow,executors)
                    self.assertEqual(len(calls),1)
                    self.assertEqual(result.terminal_state,'accepted' if repaired else 'escalated')
                    sequence=[]
                    for event in result.events:
                        item=asdict(event); rid=item.pop('run_id')
                        # Separate executions have independent clock samples.
                        item.pop('occurred_at_unix_ns'); item.pop('duration_ns')
                        for key in ('state_visit_id','invocation_id'):
                            if item[key]: item[key]=item[key].replace(rid,'RUN')
                        sequence.append(item)
                    sequences.append(sequence)
                self.assertEqual(*sequences)
