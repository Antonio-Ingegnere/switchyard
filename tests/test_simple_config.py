"""CONFIG-001: one file, existing compiler/runtime/adapters and real Git/verifier."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flow_config import FlowConfigError, _yaml_data
from switchyard.simple_config import load_simple_config, simple_config_from_text, compile_simple_config
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.workflow_execution import execute_v2_workflow
from switchyard.qa_lifecycle import create_qa_candidate, accept_v2_work, reopen_v2_work
from examples.delivery.offline_lifecycle import FIX, git

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT/'examples/simple/config.yaml'


def parse(data):
    return simple_config_from_text(json.dumps(data))  # JSON is strict YAML; no aliases from shared dicts.


class SimpleConfigTests(unittest.TestCase):
    def setUp(self):
        self.data = _yaml_data(EXAMPLE.read_text())

    def test_default_feature_equivalence(self): self.equivalent('feature')
    def test_default_bugfix_equivalence(self): self.equivalent('bugfix')

    def equivalent(self, name):
        before = load_workflow_bundle(ROOT/'examples/delivery/workflows-v2.yaml')
        simple = parse(self.data); after = compile_simple_config(simple)
        a, b = before.flows[name], after.flows[name]
        self.assertEqual(a.entry,b.entry); self.assertEqual(a.run_budget,b.run_budget)
        self.assertEqual(set(a.states),set(b.states))
        for state, old in a.states.items():
            new=b.states[state]
            for field in ('on','consumes','produces','terminal','budget','escalation_exhausted'):
                self.assertEqual(getattr(old,field),getattr(new,field),(state,field))
            expected = ('repo_read','command') if state in ('SPECIFY','TRIAGE','SPECIFY_FIX') else old.capabilities
            self.assertEqual(new.capabilities,expected)
            if old.terminal: continue
            left,right=before.roles[old.role],after.roles[new.role]
            old_rungs=[left.primary,*[s.binding for s in left.escalation]]
            new_rungs=[right.primary,*[s.binding for s in right.escalation]]
            self.assertEqual(len(old_rungs),len(new_rungs))
            for x,y in zip(left.escalation,right.escalation):
                self.assertEqual((x.kind,x.on,x.context),(y.kind,y.on,y.context))
            for x,y in zip(old_rungs,new_rungs):
                self.assertEqual((x.budget,x.outcomes),(y.budget,y.outcomes))
                oldp,newp=before.profiles[x.profile],after.profiles[y.profile]
                if state in ('SPECIFY','TRIAGE','SPECIFY_FIX'):
                    self.assertEqual((newp.provider,newp.model,y.effort),('codex','gpt-6-astra','high'))
                else:
                    self.assertEqual(oldp.provider,newp.provider)
                    self.assertEqual(newp.model,'gpt-6-astra' if oldp.model=='codex' else oldp.model)
                    self.assertEqual(x.effort,y.effort)
                self.assertEqual(oldp.command,newp.command)
        used={art for s in b.states.values() for art in (*s.consumes,*s.produces)}
        self.assertEqual({k:before.artifacts[k] for k in used},{k:after.artifacts[k] for k in used})

    def test_only_shared_agent_edit_switches_all_specification(self):
        before=parse(self.data).show()['invocations']
        self.data['agents']['spec'].update(provider='claude',model='sonnet')
        after=parse(self.data).show()['invocations']
        changed=[]
        for a,b in zip(before,after):
            if a!=b:
                changed.append((b['flow'],b['state']))
                self.assertEqual((b['provider'],b['model'],b['effort']),('claude','sonnet','high'))
        self.assertEqual(set(changed),{('feature','SPECIFY'),('bugfix','TRIAGE'),('bugfix','SPECIFY_FIX')})

    def test_only_state_reference_edit_changes_one_state(self):
        before=parse(self.data).show()['invocations']
        self.data['flows']['feature']['states']['SPECIFY']['agent']='diagnose-deep'
        after=parse(self.data).show()['invocations']
        self.assertEqual([(a['flow'],a['state']) for a,b in zip(before,after) if a!=b],[('feature','SPECIFY')])

    def test_permissions_fail_before_executable_resolution(self):
        self.data['agents']['spec']['shell']=False
        with patch('switchyard.provider_stages._executable',side_effect=AssertionError('must not resolve')):
            with self.assertRaisesRegex(FlowConfigError,'agents.spec.*codex.*workspace=read, shell=false'):parse(self.data)

    def test_provider_neutral_permission_matrix(self):
        for provider in ('claude','codex'):
            for workspace in ('none','read','write'):
                for shell in (True,False):
                    with self.subTest(provider=provider,workspace=workspace,shell=shell):
                        d={'agents':{'a':dict(provider=provider,model='sonnet' if provider=='claude' else 'direct-model',workspace=workspace,shell=shell)},
                           'flows':{'f':{'start':'go','states':{'NEEDS_HUMAN': {'terminal':True}, 'READY_FOR_MANUAL_QA': {'terminal':True}, 'go':{'agent':'a','on':{'ready':'READY_FOR_MANUAL_QA'}}}}}}
                        allowed = (not shell if workspace=='none' else provider=='claude' or shell)
                        if allowed: parse(d)
                        else:
                            with self.assertRaisesRegex(FlowConfigError,'cannot represent'):parse(d)

    def test_missing_and_later_producer_rejected(self):
        for name in ('unknown','diagnosis'):
            with self.subTest(name=name):
                d=copy.deepcopy(self.data);d['flows']['bugfix']['states']['TRIAGE']['consumes'].append(name)
                with self.assertRaisesRegex(FlowConfigError,'consumes.*(undeclared|reachable producer)'):parse(d)

    def test_self_producer_cannot_supply_first_input(self):
        self.data['flows']['feature']['states']['SPECIFY']['consumes'].append('microspec')
        with self.assertRaisesRegex(FlowConfigError,'no reachable producer'):parse(self.data)

    def test_conflicting_output_rejected(self):
        self.data['flows']['feature']['states']['REPAIR_ONCE']['produces']['implementation_handoff']['format']='text'
        with self.assertRaisesRegex(FlowConfigError,'produces.implementation_handoff.*conflicting'):parse(self.data)

    def test_strict_yaml_and_unknown_fields(self):
        for text in ('agents: {}\nagents: {}\nflows: {}', 'agents: &x {}\nflows: *x', 'agents: {}\nflows: {}\nroles: {}'):
            with self.subTest(text=text), self.assertRaises(FlowConfigError):simple_config_from_text(text)

    def test_immutable_request(self):
        self.data['flows']['feature']['states']['SPECIFY']['produces']['request']={'format':'markdown'}
        with self.assertRaisesRegex(FlowConfigError,'immutable request'):parse(self.data)

    def test_unknown_agent_and_target(self):
        for field,value in [('agent','missing'),('on',{'ready':'TYPO'})]:
            d=copy.deepcopy(self.data);d['flows']['feature']['states']['SPECIFY'][field]=value
            with self.subTest(field=field), self.assertRaisesRegex(FlowConfigError,'flows.feature.states.SPECIFY.*unknown'):parse(d)

    def test_budget_types_and_command_fields(self):
        for value in (True,0,-1,'2'):
            d=copy.deepcopy(self.data);d['flows']['feature']['limits']['run_invocations']=value
            with self.subTest(value=value), self.assertRaisesRegex(FlowConfigError,'limits.run_invocations.*positive'):parse(d)
        self.data['agents']['tests']['shell']=True
        with self.assertRaisesRegex(FlowConfigError,'agents.tests.shell.*unknown'):parse(self.data)

    def test_escalation_context_and_permission_invariance(self):
        diag=self.data['flows']['bugfix']['states']['DIAGNOSE']
        diag['run'][1]['context']='fresh'
        with self.assertRaisesRegex(FlowConfigError,r'DIAGNOSE.run\[1\].context.*continue'):parse(self.data)
        diag['run'][1]['context']='continue';diag['run'][2]['agent']='code'
        with self.assertRaisesRegex(FlowConfigError,'identical workspace/shell'):parse(self.data)

    def test_singleton_outcome_ladder_and_runtime_adjacent_routing(self):
        from runtime import run,WorkUnit,ExecutorResult,InvocationResponse
        d={'agents':{k:v for k,v in self.data['agents'].items() if k != 'tests'},'flows':{'proof':{'start':'go','states':{'NEEDS_HUMAN': {'terminal':True}, 'READY_FOR_MANUAL_QA': {'terminal':True}, 'go':{
            'run':[{'agent':'investigate','outcomes':['inconclusive']},
                   {'agent':'diagnose-deep','when':['inconclusive'],'context':'continue','outcomes':['diagnosis_disproved']},
                   {'agent':'independent','when':['diagnosis_disproved'],'context':'fresh','outcomes':['ready']}],
            'on':{'ready':'READY_FOR_MANUAL_QA'}}}}}}
        class E:
            def __init__(self,b):self.b=b
            def run(self,request):return InvocationResponse(ExecutorResult(self.b.executor_allowed_outcomes[0]))
        c=compile_workflow(parse(d).bundle,'proof',E)
        bs=[p.binding for p in c.plan.values() if p.binding]
        self.assertEqual([b.executor_allowed_outcomes for b in bs],[('inconclusive',),('diagnosis_disproved',),('ready',)])
        self.assertEqual([b.context_mode for b in bs],[None,'continue','fresh'])
        result=run(WorkUnit('facts'),c.flow,c.executors)
        self.assertEqual(result.terminal_state,'READY_FOR_MANUAL_QA')
        self.assertEqual(len([e for e in result.events if e.kind=='invocation_started']),3)

    def test_run_budget_routes_without_extra_invocation(self):
        from runtime import run, WorkUnit, ExecutorResult, InvocationResponse
        self.data['flows']['feature']['limits']['run_invocations']=1
        class E:
            def run(self,request):return InvocationResponse(ExecutorResult('ready'))
        compiled=compile_workflow(parse(self.data).bundle,'feature',lambda binding:E())
        result=run(WorkUnit('task'),compiled.flow,compiled.executors)
        self.assertEqual(result.terminal_state,'NEEDS_HUMAN')
        self.assertEqual(len([e for e in result.events if e.kind=='invocation_started']),1)

    def test_bad_command_uses_simple_agent_location(self):
        self.data['agents']['tests']['command']='python3 tests'
        with self.assertRaisesRegex(FlowConfigError,'agents.tests.command'):parse(self.data)

    def test_lifecycle_policy(self):
        self.data['lifecycle']={'manual_qa':False}
        self.assertEqual(parse(self.data).show()['lifecycle'],dict(manual_qa=False,commit=False,reopen=False))
        self.data['lifecycle']['accept']={'commit':True}
        with self.assertRaisesRegex(FlowConfigError,'require manual_qa'):parse(self.data)

    def test_cli_default_path_show_validate_no_executables(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);(root/'.switchyard').mkdir();shutil.copyfile(EXAMPLE,root/'.switchyard/config.yaml')
            env={**os.environ,'PYTHONPATH':str(ROOT),'PATH':''}
            for action in ('show','validate'):
                result=subprocess.run([sys.executable,'-m','switchyard','config',action],cwd=root,env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                if action=='show':self.assertEqual(json.loads(result.stdout),load_simple_config(EXAMPLE).show())


class OneFileIntegration(unittest.TestCase):
    def test_feature_one_file_accept(self):self.delivery('feature','accept')
    def test_bugfix_one_file_accept(self):self.delivery('bugfix','accept')
    def test_feature_one_file_reopen(self):self.delivery('feature','reopen')

    def delivery(self,flow,operation):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();target=root/'target'
            shutil.copytree(ROOT/'tests/fixtures'/f'{flow}_dogfood_project',target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            (target/'.switchyard').mkdir();shutil.copyfile(EXAMPLE,target/'.switchyard/config.yaml')
            (target/'.gitignore').write_text('__pycache__/\n*.pyc\n')
            source='catalog.py' if flow=='feature' else 'task_queue.py'
            for args in [('init','-b','main'),('config','user.name','Offline'),('config','user.email','offline@example.invalid'),('config','commit.gpgsign','false'),
                         ('add','--','.gitignore','.switchyard/config.yaml',source,'tests'),('commit','-m','baseline')]:git(target,*args)
            base=git(target,'rev-parse','HEAD')
            fix=FIX if flow=='feature' else (target/source).read_text().replace('self._pending = []','self._pending = []\n        self._sequence = 0').replace('(priority, len(self._pending), task))','(priority, self._sequence, task))\n        self._sequence += 1')
            script=(ROOT/'tests/fixtures/provider_cli.py').read_text().replace("outcome=plan.get", "if state in ('IMPLEMENT','FIX'):\n    Path("+repr(source)+").write_text("+repr(fix)+")\noutcome=plan.get")
            for provider in ('claude','codex'):
                path=root/provider;path.write_text('#!'+sys.executable+'\n'+script);path.chmod(0o755)
            (root/'plan').write_text('{"artifacts":{}}')
            env={**os.environ,'FAKE_CONTRACT_AWARE':'1','FAKE_PLAN':str(root/'plan'),'FAKE_LOG':str(root/'calls'),
                 'PYTHONDONTWRITEBYTECODE':'1','PATH':str(Path(sys.executable).parent)+os.pathsep+os.defpath}
            config=load_simple_config(target/'.switchyard/config.yaml')
            execution=execute_v2_workflow(config.bundle,flow,'Human request',root/'work',
                ProviderRuntimeConfig(str(target),str(root/'claude'),str(root/'codex'),environment=env))
            self.assertEqual(execution.result.terminal_state,'READY_FOR_MANUAL_QA')
            self.assertEqual(git(target,'rev-parse','HEAD'),base)
            self.assertIn('OK',execution.workspace.read_artifact('verifier_evidence'))
            self.assertEqual(list((target/'.switchyard').iterdir()),[target/'.switchyard/config.yaml'])
            calls=(root/'calls').read_bytes()
            with patch('switchyard.provider_stages.execute',side_effect=AssertionError('no QA models')):
                candidate=create_qa_candidate(target,execution.workspace,execution.result,flow,root/'qa')
                result=accept_v2_work(target,root/'qa') if operation=='accept' else reopen_v2_work(target,root/'qa','Human feedback')
            self.assertEqual((root/'calls').read_bytes(),calls)
            self.assertEqual(result.terminal,'ACCEPTED' if operation=='accept' else 'REOPENED')
            if operation=='accept':
                self.assertEqual(git(target,'rev-parse','HEAD^'),base)
                self.assertEqual(git(target,'diff-tree','--no-commit-id','--name-only','-r','HEAD'),source)
                self.assertEqual(git(target,'status','--porcelain'),'')
            else:self.assertEqual(git(target,'rev-parse','HEAD'),base)


if __name__=='__main__':unittest.main()
