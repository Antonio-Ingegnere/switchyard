"""Explicit lifecycle across config, compiled runtime, durable recovery and legacy reads."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from flow_config import FlowConfigError
from runtime import Flow, State, WorkUnit, ExecutorResult, InvocationResponse, run, validate, FlowValidationError
from switchyard.cli import main
from switchyard.context_transport import StageResult
from switchyard.legacy_config import read_legacy_project
from switchyard.simple_config import simple_config_from_text
from switchyard.workflow_compiler import compile_workflow
from switchyard.work_contract import WorkWorkspace
from switchyard import recovery
import test_human_input
import test_terminal_recovery
import test_recovery


def config():
    return dict(agents={'worker': dict(provider='codex', model='offline', workspace='none', shell=False)},
        flows={'feature': dict(start='STEP', limits=dict(run_invocations=4, on_exceeded='BANANA'), states={
            'STEP': dict(agent='worker', on={'ready':'BANANA'}, budget={'on_exceeded':'BANANA'}),
            'BANANA': dict(terminal=True)})})


def parse(data):
    return simple_config_from_text(json.dumps(data))


class Fixed:
    def __init__(self, outcome='ready'): self.outcome = outcome
    def run(self, request): return InvocationResponse(ExecutorResult(self.outcome))


class TerminalContractConfigTests(unittest.TestCase):
    def test_arbitrary_true_terminal_and_execution_compile(self):
        project = parse(config())
        compiled = compile_workflow(project.bundle, 'feature', lambda b: Fixed())
        self.assertEqual(compiled.flow.states['BANANA'].kind, 'terminal')
        self.assertEqual(compiled.flow.states[compiled.entry_states['STEP']].kind, 'execution')
        result = run(WorkUnit('request'), compiled.flow, compiled.executors)
        self.assertEqual(result.terminal_state, 'BANANA')
        self.assertFalse(result.paused)
        self.assertEqual(sum(e.kind == 'invocation_started' for e in result.events), 1)

    def test_recoverable_terminal_compiles_fixed_target(self):
        data = config()
        data['flows']['feature']['states']['BANANA']['resume_to'] = 'STEP'
        project = parse(data)
        compiled = compile_workflow(project.bundle, 'feature', lambda b: Fixed())
        self.assertEqual(compiled.flow.states['BANANA'].kind, 'recoverable_terminal')
        self.assertEqual(compiled.flow.states['BANANA'].resume_to, compiled.entry_states['STEP'])
        shown = project.show()['flows']['feature']['states']['BANANA']
        self.assertEqual((shown['state_kind'], shown['resume_to']), ('recoverable_terminal','STEP'))

    def test_missing_named_target_is_never_inferred(self):
        for name in ('NEEDS_HUMAN','NEEDS_REDIAGNOSIS','NEEDS_SOMETHING','READY_FOR_MANUAL_QA','FAILED_FINAL','DONE'):
            data = config()
            data['flows']['feature']['states']['STEP']['on']['ready'] = name
            with self.subTest(name=name), self.assertRaisesRegex(FlowConfigError, 'flows.feature.states.STEP.*unknown target'):
                parse(data)

    def test_terminal_looking_execution_name_still_invokes_executor(self):
        for name in ('NEEDS_HUMAN', 'NEEDS_REDIAGNOSIS', 'NEEDS_SOMETHING', 'FAILED_FINAL', 'DONE'):
            data = config(); flow = data['flows']['feature']
            flow['states'][name] = flow['states'].pop('STEP'); flow['start'] = name
            compiled = compile_workflow(parse(data).bundle,'feature',lambda b:Fixed())
            self.assertEqual(compiled.flow.states[compiled.entry_states[name]].kind,'execution')
            result = run(WorkUnit('request'),compiled.flow,compiled.executors)
            self.assertEqual(sum(e.kind=='invocation_started' for e in result.events),1)

    def test_incomplete_terminal_looking_state_rejected(self):
        data = config(); data['flows']['feature']['states']['BANANA'] = {}
        with self.assertRaisesRegex(FlowConfigError,'flows.feature.states.BANANA.*missing'):
            parse(data)

    def test_invalid_lifecycle_combinations(self):
        invalid = [
            dict(terminal=True, agent='worker'),
            dict(terminal=True, on={'ready':'STEP'}),
            dict(terminal=False),
            dict(terminal=True, wait_for='human_input', resume_to='STEP', budget={'visits':1,'on_exceeded':'BANANA'}),
            dict(wait_for='human_input', agent='worker', resume_to='STEP', budget={'visits':1,'on_exceeded':'BANANA'}),
            dict(wait_for='human_input', budget={'visits':1,'on_exceeded':'BANANA'}),
            dict(wait_for='human_input', resume_to='MISSING', budget={'visits':1,'on_exceeded':'BANANA'}),
            dict(wait_for='human_input', resume_to='STEP'),
            dict(wait_for='timer', resume_to='STEP', budget={'visits':1,'on_exceeded':'BANANA'}),
            dict(terminal=True, resume_to='MISSING'),
            dict(terminal=True, resume_to='BANANA'),
            dict(terminal=True, resume_to=None),
            dict(agent='worker', on={'ready':'BANANA'}, resume_to='STEP'),
        ]
        for declaration in invalid:
            data = config()
            data['flows']['feature']['states']['INVALID'] = declaration
            data['flows']['feature']['states']['STEP']['on']['ready'] = 'INVALID'
            with self.subTest(declaration=declaration), self.assertRaisesRegex(FlowConfigError,'flows.feature.states.INVALID'):
                parse(data)

    def test_wait_and_recovery_cannot_target_wait(self):
        data = config(); states = data['flows']['feature']['states']
        states['DECISION'] = dict(wait_for='human_input', resume_to='STEP', budget={'visits':1,'on_exceeded':'BANANA'})
        states['STEP']['on']['question'] = 'DECISION'
        for declaration in (dict(terminal=True,resume_to='DECISION'),
                            dict(wait_for='human_input',resume_to='DECISION',budget={'visits':1,'on_exceeded':'BANANA'})):
            states['INVALID'] = declaration; states['STEP']['on']['ready'] = 'INVALID'
            with self.subTest(declaration=declaration), self.assertRaisesRegex(FlowConfigError,'flows.feature.states.INVALID.resume_to'):
                parse(data)

    def test_runtime_refuses_arbitrary_goto_and_invalid_terminal(self):
        flow = Flow('A', {'A':State('worker',{'ready':'STOP'}),
                         'B':State('worker',{'ready':'STOP'}),
                         'STOP':State(terminal=True,resume_to='A')})
        result = run(WorkUnit('request'),flow,{'worker':Fixed()})
        with self.assertRaisesRegex(ValueError,'declared executable target'):
            run(WorkUnit('request'),flow,{'worker':Fixed()},resume_from=result.events,recover_to='B')
        plain = Flow('A',dict(flow.states,STOP=State(terminal=True)))
        with self.assertRaisesRegex(ValueError,'declared executable target'):
            run(WorkUnit('request'),plain,{'worker':Fixed()},resume_from=result.events,recover_to='A')
        for bad in (State(terminal=True,resume_to='missing'), State('worker',terminal=True)):
            with self.assertRaises(FlowValidationError):
                validate(Flow('A',dict(flow.states,STOP=bad)),{'worker':Fixed()})

    def test_cli_validates_and_shows_lifecycle(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'; data = config()
            path.write_text(json.dumps(data))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['config','show',str(path)]),0)
            self.assertEqual(json.loads(out.getvalue())['flows']['feature']['states']['BANANA']['state_kind'],'terminal')
            del data['flows']['feature']['states']['BANANA'];path.write_text(json.dumps(data))
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertNotEqual(main(['config','validate',str(path)]),0)
            self.assertIn('flows.feature',err.getvalue())

    def test_current_version_rejects_legacy_opt_in(self):
        for version in (1,True,3,'2'):
            data = config(); data['version'] = version
            with self.subTest(version=version), self.assertRaisesRegex(FlowConfigError,'version'):
                parse(data)
        data = config();data['version'] = 2
        self.assertEqual(parse(data).bundle.flow('feature').states['BANANA'].kind,'terminal')

    def test_all_current_example_contracts_validate(self):
        from switchyard.workflow_bundle import load_workflow_bundle
        from switchyard.simple_config import load_simple_config
        root = Path(__file__).resolve().parents[1]
        bundles = [load_simple_config(root/'examples/simple/config.yaml').bundle]
        bundles += [load_workflow_bundle(path) for path in (root/'examples').rglob('workflows-v2.yaml')]
        for bundle in bundles:
            for flow_name in bundle.flows:
                compiled = compile_workflow(bundle,flow_name,lambda b:Fixed())
                for plan in compiled.plan.values():
                    self.assertEqual(plan.state.kind,plan.declaration.kind)

    def test_recovery_only_reachable_execution_is_valid(self):
        data = config();flow = data['flows']['feature']
        flow['start'] = 'BANANA';flow['states']['BANANA']['resume_to'] = 'STEP'
        self.assertEqual(parse(data).bundle.flow('feature').states['BANANA'].kind,'recoverable_terminal')


class TerminalContractHumanTests(unittest.TestCase):
    base = test_human_input.HumanInputTests
    setUp, setUp_base, git, save_config = base.setUp, base.setUp_base, base.git, base.save_config
    start, answer, history, record_pair = base.start, base.answer, base.history, base.record_pair

    def test_renamed_human_wait_same_work_sources_charges_and_multiple_rounds(self):
        for name in ('WAITING_FOR_HUMAN', 'PRODUCT_DECISION_REQUIRED'):
            with self.subTest(name=name):
                self.work = self.root/name
                declaration = self.states.pop('DECISION', self.states.pop('WAITING_FOR_HUMAN', None))
                self.states[name] = declaration
                self.states['SPECIFY']['on']['needs_clarification'] = name
                self.text = self.save_config()
                self.stages.rounds=2;self.stages.spec_calls=0;self.stages.calls=[]
                execution = self.start()
                status = recovery.recovery_status(self.work)
                self.assertEqual((status['state_kind'],status['workflow_terminal'],status['wait_for'],status['resume_to']),
                                 ('wait',False,'human_input','SPECIFY'))
                self.assertTrue(status['waiting']);self.assertTrue(execution.result.paused)
                self.assertEqual(len(self.stages.calls),1)
                with self.assertRaisesRegex(recovery.RecoveryBlocked,'human_input_required'):
                    recovery.resume_v2_work(self.work,self.text,self.config)
                before = self.history()
                self.answer()
                self.assertEqual(len(self.record_pair()[0]),2)
                result = self.answer()
                self.assertEqual(self.history()[:len(before)],before)
                self.assertEqual(len({e.run_id for e in self.history()}),1)
                self.assertEqual(sum(e.kind=='invocation_finished' for e in self.history()),5)
                self.assertEqual(result.result.terminal_state,'READY_FOR_MANUAL_QA')
                resumed = [i for i in self.stages.calls if i.binding.logical_state=='SPECIFY'][1:]
                self.assertTrue(all(i.sources and i.human_clarification for i in resumed))
                self.assertTrue(all(test_human_input.ANSWER in i.human_clarification[1].content for i in resumed))


class TerminalContractRecoveryTests(unittest.TestCase):
    base = test_terminal_recovery.TerminalRecoveryTests
    setUp, git, save, states, events = base.setUp, base.git, base.save, base.states, base.events

    def test_arbitrary_recoverable_terminal_uses_frozen_route(self):
        states = self.data['flows']['feature']['states']
        states['RECHECK_REQUIRED'] = states.pop('NEEDS_REDIAGNOSIS')
        states['VERIFY']['on']['fail'] = 'RECHECK_REQUIRED'
        self.text = self.save()
        recovery.execute_recoverable_v2_work(self.text,'feature','request',self.work,self.config)
        before = self.events()
        status = recovery.recovery_status(self.work)
        self.assertEqual((status['state_kind'],status['resume_to'],status['resume_allowed']),
                         ('recoverable_terminal','VERIFY',True))
        self.assertEqual(status['recovery_source'],'workflow')
        self.assertTrue(status['workflow_terminal']);self.assertFalse(status['waiting'])
        states['RECHECK_REQUIRED']['resume_to'] = 'IMPLEMENT'; changed = self.save()
        # Status reads immutable baseline, not the edited project file.
        self.assertEqual(recovery.recovery_status(self.work)['resume_to'],'VERIFY')
        with self.assertRaisesRegex(recovery.RecoveryBlocked,'semantic_configuration_changed'):
            recovery.resume_v2_work(self.work,changed,self.config)
        states['RECHECK_REQUIRED']['resume_to'] = 'VERIFY'; self.text = self.save()
        self.gate.write_text('ready')
        result = recovery.resume_v2_work(self.work,self.text,self.config)
        self.assertEqual(result.result.terminal_state,'READY_FOR_MANUAL_QA')
        self.assertEqual(self.events()[:len(before)],before)
        self.assertEqual(sum(e.kind=='invocation_finished' for e in self.events()),4)
        baseline = json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('baseline'))
        self.assertEqual(baseline['lifecycle_contract_version'],2)
        self.assertEqual(baseline['config'],self.text)

    def test_arbitrary_true_terminal_cannot_resume(self):
        states = self.data['flows']['feature']['states']
        del states['NEEDS_REDIAGNOSIS']
        states['BANANA'] = dict(terminal=True);states['VERIFY']['on']['fail'] = 'BANANA'
        self.text = self.save()
        recovery.execute_recoverable_v2_work(self.text,'feature','request',self.work,self.config)
        status = recovery.recovery_status(self.work)
        self.assertEqual((status['terminal'],status['state_kind'],status['resume_allowed'],status['resume_to']),
                         ('BANANA','terminal',False,None))
        with self.assertRaisesRegex(recovery.RecoveryBlocked,'no_declared_recovery_route'):
            recovery.resume_v2_work(self.work,self.text,self.config)


class TerminalContractLegacyTests(unittest.TestCase):
    base = test_recovery.RecoveryTests
    setUp, git, save_config = base.setUp, base.git, base.save_config

    def test_legacy_frozen_work_read_status_recover_without_rewriting_history(self):
        text = (Path(__file__).parent/'fixtures/legacy_project_config.json').read_text()
        with self.assertRaisesRegex(FlowConfigError,'unknown target'):
            simple_config_from_text(text)  # Same ambiguous input cannot start current work.
        project = read_legacy_project(text)
        outcome = ['stop']
        class Stage:
            def run(self, invocation): return StageResult(outcome[0])
        # Model the previous writer: original config and baseline with no version marker.
        # Construct once; no files are altered or migrated after publication.
        put = recovery._put
        def legacy_put(store, name, data):
            if name == 'baseline':
                data = {k:v for k,v in data.items() if k!='lifecycle_contract_version'}
            return put(store,name,data)
        with patch('switchyard.recovery.ProviderStageExecutorResolver',return_value=lambda binding:Stage()):
            with recovery._lock(self.work):
                store = WorkWorkspace.open(self.work/'recovery',recovery.CATALOG)
                store.initialize_request('legacy request')
                repo = recovery._Repo(self.target)
                with patch('switchyard.recovery._put',legacy_put):
                    recovery._baseline(repo,store,text,'feature')
                recovery._operation(store,'running')
                recovery._drive(project,'feature','legacy request',self.work,self.config,store,repo)
            baseline = store.read_artifact('baseline')
            self.assertNotIn('lifecycle_contract_version',json.loads(baseline))
            before = {p:p.read_bytes() for p in self.work.rglob('*') if p.is_file()}
            self.assertEqual(WorkWorkspace.open(self.work).read_artifact('request'),'legacy request')
            for _ in range(2):
                status = recovery.recovery_status(self.work)
                self.assertEqual((status['state_kind'],status['resume_to'],status['resume_allowed']),
                                 ('recoverable_terminal','STEP',True))
            self.assertEqual(before,{p:p.read_bytes() for p in self.work.rglob('*') if p.is_file()})
            outcome[0] = 'ready'
            result = recovery.resume_v2_work(self.work,text,self.config)
            self.assertEqual(result.result.terminal_state,'READY_FOR_MANUAL_QA')
            self.assertEqual(store.read_artifact('baseline'),baseline)
            for path, content in before.items():
                self.assertTrue(path.read_bytes().startswith(content),str(path))  # Append only.
            self.assertEqual(len({e.run_id for e in result.result.events}),1)

    def test_unknown_frozen_contract_version_fails_closed(self):
        with self.assertRaisesRegex(recovery.RecoveryBlocked,'unsupported_lifecycle_contract_version'):
            recovery._frozen_project(dict(config='ignored',lifecycle_contract_version=999))


if __name__ == '__main__':
    unittest.main()
