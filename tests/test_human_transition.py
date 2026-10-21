"""HUMAN-TRANSITION: declared human decisions, durable restart and bounded context."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from runtime import Flow, State, WorkUnit, ExecutorResult, InvocationResponse, run, validate, FlowValidationError, ExecutionError
from switchyard.cli import main
from switchyard.context_transport import StageResult, StageHandoff, ProducedArtifact
from switchyard.human_input import records, store_for, write_answer, clarification_snapshots, request_targets
from switchyard.observability import read_events, summarize_runs
from switchyard.provider_process import ProviderExecutionError
from switchyard.recovery import execute_recoverable_v2_work, answer_v2_work, resume_v2_work, recovery_status, RecoveryBlocked
from switchyard.simple_config import simple_config_from_text
from switchyard.workflow_compiler import compile_workflow
from switchyard.work_contract import WorkWorkspace, WorkContractError
import test_recovery

QUESTION = "Which build and environment did the reporter use?"
ANSWER = "The reporter was running a stale build; fresh build reproduces differently."


class ReviewStages:
    def __init__(self):
        self.calls = []
        self.ask_again = False
        self.fail_next = False

    def __call__(self, binding):
        owner = self
        class Stage:
            def run(self, invocation):
                owner.calls.append(invocation)
                if owner.fail_next:
                    owner.fail_next = False
                    raise ProviderExecutionError('provider exited with status 1', returncode=1)
                name = binding.logical_state
                if name == 'REPRODUCE':
                    outcome = 'reproduced' if invocation.human_clarification else 'inconclusive'
                    return StageResult(outcome, (ProducedArtifact('evidence', 'original reproduction evidence'),),
                                       StageHandoff('Build needs review', blockers=(QUESTION,)))
                if name == 'DIAGNOSE':
                    if owner.ask_again:
                        owner.ask_again = False
                        return StageResult('inconclusive', handoff=StageHandoff('Check the fresh build', blockers=('Please reproduce again',)))
                    return StageResult('diagnosed', handoff=StageHandoff('Fresh build diagnosed'))
                return StageResult('ready', handoff=StageHandoff('Fixed'))
        return Stage()


class HumanTransitionTests(unittest.TestCase):
    setUp_base = test_recovery.RecoveryTests.setUp
    git = test_recovery.RecoveryTests.git
    save_config = test_recovery.RecoveryTests.save_config

    def setUp(self):
        self.setUp_base()
        del self.data['agents']['tests']
        self.states = {
            'REPRODUCE': dict(agent='spec', consumes=['request'], produces={'evidence': {'format':'markdown'}},
                              on={'reproduced':'FIX','inconclusive':'HUMAN_REVIEW'}, budget={'visits':3,'on_exceeded':'BLOCKED'}, rung_budget_scope='visit'),
            'HUMAN_REVIEW': dict(wait_for='human_input', resume_to=['REPRODUCE','DIAGNOSE'],
                                 budget={'visits':3,'on_exceeded':'BLOCKED'}),
            'DIAGNOSE': dict(agent='spec', consumes=['request','evidence'], on={'diagnosed':'FIX','inconclusive':'HUMAN_REVIEW'},
                            budget={'visits':3,'on_exceeded':'BLOCKED'}, rung_budget_scope='visit'),
            'FIX': dict(agent='code', consumes=['evidence'], on={'ready':'DONE'}, budget={'invocations':1,'on_exceeded':'BLOCKED'}),
            'DONE': dict(terminal=True), 'BLOCKED': dict(terminal=True),
        }
        self.data['flows'] = {'feature': dict(start='REPRODUCE', limits={'run_invocations':10,'on_exceeded':'BLOCKED'}, states=self.states)}
        self.text = self.save_config()
        self.stages = ReviewStages()
        self.resolver = patch('switchyard.recovery.ProviderStageExecutorResolver', return_value=self.stages)
        self.resolver.start(); self.addCleanup(self.resolver.stop)

    def start(self):
        return execute_recoverable_v2_work(self.text, 'feature', 'Investigate reported failure', self.work, self.config)

    def answer(self, target=None, text=ANSWER):
        return answer_v2_work(self.work, recovery_status(self.work)['human_input_request_id'], text,
                              self.text, self.config, resume_to=target)

    def history(self):
        return tuple(read_events(self.work/'events.jsonl'))

    def pair(self):
        return records(WorkWorkspace.open(self.work))

    def test_incident_declared_selection_keeps_work_artifacts_events_and_accounting(self):
        first = self.start()
        self.assertTrue(first.result.paused)
        status = recovery_status(self.work)
        self.assertEqual(status['resume_targets'], ['REPRODUCE','DIAGNOSE'])
        self.assertIsNone(status['resume_to'])
        self.assertTrue(status['resume_allowed'])
        self.assertEqual((status['question'],status['origin_state'],status['origin_outcome']), (QUESTION,'REPRODUCE','inconclusive'))
        before = self.history()
        workspace = WorkWorkspace.open(self.work)
        evidence = workspace.versions('evidence')
        result = self.answer('DIAGNOSE')
        self.assertEqual(result.result.terminal_state, 'DONE')
        self.assertEqual(self.history()[:len(before)], before)
        self.assertEqual(len({e.run_id for e in self.history()}), 1)
        self.assertEqual(recovery_status(self.work)['work_id'], self.work.name)
        self.assertEqual(workspace.versions('evidence'), evidence)
        self.assertEqual([i.binding.logical_state for i in self.stages.calls], ['REPRODUCE','DIAGNOSE','FIX'])
        self.assertEqual(len(self.stages.calls[1].human_clarification), 2)
        self.assertIn(ANSWER, self.stages.calls[1].human_clarification[1].content)
        self.assertFalse(self.stages.calls[2].human_clarification)
        self.assertFalse(clarification_snapshots(workspace,'feature','REPRODUCE'))
        q,a = self.pair()
        self.assertEqual((q[0]['schema_version'],a[0]['schema_version']), (2,2))
        self.assertEqual(a[0]['selected_resume_to'], 'DIAGNOSE')
        selected = [e.target for e in self.history() if e.kind in ('human_input_received','human_input_resumed')]
        self.assertTrue(all('DIAGNOSE' in t for t in selected))
        self.assertEqual(summarize_runs(self.history())[0].invocation_count, 3)
        self.assertEqual(recovery_status(self.work)['selected_resume_to'], 'DIAGNOSE')

    def test_invalid_or_missing_selection_has_no_writes(self):
        self.start()
        before = {str(p.relative_to(self.work)):p.read_bytes() for p in self.work.rglob('*') if p.is_file()}
        for target in (None,'VERIFY','BLOCKED','HUMAN_REVIEW','unknown','',[],False):
            with self.subTest(target=target), self.assertRaisesRegex(WorkContractError, 'REPRODUCE, DIAGNOSE'):
                self.answer(target)
            self.assertEqual(self.pair()[1], ())
        after = {str(p.relative_to(self.work)):p.read_bytes() for p in self.work.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.answer('DIAGNOSE')

    def test_scalar_and_single_item_list_accept_old_api(self):
        for route in ('REPRODUCE',['REPRODUCE']):
            with self.subTest(route=route):
                self.work = self.root/('scalar' if isinstance(route,str) else 'list')
                # Keep DIAGNOSE reachable via semantic routing as it is no longer a wait target.
                self.states['REPRODUCE']['on']['diagnose'] = 'DIAGNOSE'
                self.states['HUMAN_REVIEW']['resume_to'] = route
                self.text = self.save_config()
                self.start()
                self.assertEqual(recovery_status(self.work)['resume_to'],'REPRODUCE')
                self.assertEqual(self.answer().result.terminal_state,'DONE')

    def test_wait_availability_is_independent_of_target_count_and_answer(self):
        for targets in ('REPRODUCE', ['REPRODUCE'], ['REPRODUCE','DIAGNOSE']):
            with self.subTest(targets=targets):
                self.work=self.root/('availability-'+str(len(list(self.root.glob('availability-*')))))
                self.states['REPRODUCE']['on']['diagnose']='DIAGNOSE'
                self.states['HUMAN_REVIEW']['resume_to']=targets
                self.text=self.save_config();self.start()
                status=recovery_status(self.work)
                self.assertTrue(status['resume_allowed'])
                self.assertTrue(status['human_input_pending'])
                self.assertTrue(status['answer_allowed'])
                self.assertEqual(status['next_action'],'answer required')
                self.assertIsNone(status['resume_blocked'])
                self.assertNotIn('continuation_allowed',status)
                with self.assertRaisesRegex(RecoveryBlocked,'human_input_required'):
                    resume_v2_work(self.work,self.text,self.config)
                self.assertEqual(self.pair()[1],())
                write_answer(WorkWorkspace.open(self.work),status['human_input_request_id'],ANSWER,'REPRODUCE')
                ready=recovery_status(self.work)
                self.assertTrue(ready['resume_allowed'])
                self.assertFalse(ready['human_input_pending'])
                self.assertFalse(ready['answer_allowed'])
                self.assertEqual(ready['next_action'],'resume')
                self.assertEqual(resume_v2_work(self.work,self.text,self.config).result.terminal_state,'DONE')
                self.assertFalse(recovery_status(self.work)['resume_allowed'])

    def test_new_config_cannot_remove_or_replace_frozen_routes(self):
        self.states['REPRODUCE']['on']['diagnose']='DIAGNOSE'
        self.text=self.save_config()
        self.start();original=self.text;before=self.history()
        for targets in (['REPRODUCE'], ['REPRODUCE','FIX']):
            self.states['HUMAN_REVIEW']['resume_to']=targets
            self.text=self.save_config()
            with self.assertRaisesRegex(RecoveryBlocked,'semantic_configuration_changed'):
                self.answer('DIAGNOSE')
            self.assertEqual(recovery_status(self.work)['resume_targets'],['REPRODUCE','DIAGNOSE'])
            self.assertEqual(self.pair()[1],())
            self.assertEqual(self.history(),before)
        self.text=original;self.configfile.write_text(original)
        self.answer('DIAGNOSE')

    def test_timeout_retuning_does_not_change_frozen_destination(self):
        self.start()
        self.data['agents']['spec']['timeout']=9
        self.text=self.save_config()
        self.answer('DIAGNOSE')
        self.assertEqual(self.pair()[1][0]['selected_resume_to'],'DIAGNOSE')
        self.assertEqual(self.stages.calls[1].binding.logical_state,'DIAGNOSE')

    def test_selection_validated_against_frozen_contract(self):
        self.start()
        self.states['HUMAN_REVIEW']['resume_to'].append('FIX')
        self.text = self.save_config()
        with self.assertRaisesRegex(RecoveryBlocked,'semantic_configuration_changed'):
            self.answer('FIX')
        self.assertEqual(self.pair()[1],())
        self.assertEqual(recovery_status(self.work)['resume_targets'],['REPRODUCE','DIAGNOSE'])

    def test_two_rounds_isolate_selected_state_and_latest_pair(self):
        self.stages.ask_again = True
        self.start(); first = recovery_status(self.work)['human_input_request_id']
        self.answer('DIAGNOSE', 'First decision')
        second = recovery_status(self.work)
        self.assertNotEqual(second['human_input_request_id'],first)
        self.assertEqual(second['human_input_round'],2)
        self.answer('REPRODUCE', 'Second decision')
        q,a=self.pair()
        self.assertEqual([x['selected_resume_to'] for x in a],['DIAGNOSE','REPRODUCE'])
        self.assertEqual([x['round'] for x in q],[1,2])
        context = self.stages.calls[2].human_clarification
        self.assertEqual(len(context),2)
        self.assertIn('Second decision',context[1].content)
        self.assertNotIn('First decision',context[1].content)
        self.assertEqual(summarize_runs(self.history())[0].invocation_count,4)

    def test_durable_answer_survives_provider_failure(self):
        self.start(); self.stages.fail_next=True
        with self.assertRaises(ExecutionError): self.answer('DIAGNOSE')
        status=recovery_status(self.work)
        self.assertEqual(status['selected_resume_to'],'DIAGNOSE')
        self.assertEqual(status['human_answer'],ANSWER)
        self.assertEqual(status['infrastructure_failures_used'],1)
        self.assertEqual(next(b['used'] for b in status['semantic_budgets'] if b['scope']=='run'),1)
        self.assertEqual(resume_v2_work(self.work,self.text,self.config).result.terminal_state,'DONE')
        self.assertEqual(len(self.pair()[1]),1)
        self.assertIn(ANSWER,self.stages.calls[2].human_clarification[1].content)

    def test_restart_after_answer_before_execution(self):
        self.start()
        q = self.pair()[0][-1]
        write_answer(WorkWorkspace.open(self.work),q['request_id'],ANSWER,'DIAGNOSE')
        self.assertEqual(recovery_status(self.work)['selected_resume_to'],'DIAGNOSE')
        self.assertEqual(resume_v2_work(self.work,self.text,self.config).result.terminal_state,'DONE')
        self.assertEqual(self.stages.calls[1].binding.logical_state,'DIAGNOSE')

    def test_run_and_destination_visit_budgets_are_not_reset(self):
        self.data['flows']['feature']['limits']['run_invocations']=1
        self.text=self.save_config();self.start()
        self.assertFalse(recovery_status(self.work)['resume_allowed'])
        with self.assertRaisesRegex(RecoveryBlocked,'invocation_budget_exhausted'):self.answer('DIAGNOSE')
        self.assertEqual(self.pair()[1],())

    def test_exhausted_target_does_not_hide_other_available_target(self):
        self.states['REPRODUCE']['budget']['visits']=1
        self.text=self.save_config();self.start()
        status=recovery_status(self.work)
        self.assertTrue(status['resume_allowed'])
        self.assertEqual(status['target_blockers']['REPRODUCE'],'invocation_budget_exhausted')
        with self.assertRaisesRegex(RecoveryBlocked,'invocation_budget_exhausted'):self.answer('REPRODUCE')
        self.assertEqual(self.pair()[1],())
        self.answer('DIAGNOSE')

    def test_cli_status_and_selection(self):
        self.start(); q=self.pair()[0][0]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['work','status',str(self.work)]),0)
        self.assertEqual(json.loads(out.getvalue())['resume_targets'],['REPRODUCE','DIAGNOSE'])
        for suffix in ([],['--resume-to','VERIFY']):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(['work','answer',str(self.work),q['request_id'],ANSWER]+suffix),2)
            self.assertIn('REPRODUCE, DIAGNOSE',err.getvalue())
            self.assertEqual(self.pair()[1],())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['work','answer',str(self.work),q['request_id'],ANSWER,'--resume-to','DIAGNOSE']),0)
        self.assertEqual(json.loads(out.getvalue())['selected_resume_to'],'DIAGNOSE')

    def test_config_validation_and_compiler(self):
        original=copy.deepcopy(self.data)
        for invalid in ([],['REPRODUCE','REPRODUCE'],['REPRODUCE','unknown'],['BLOCKED'],['HUMAN_REVIEW'],
                        ['REPRODUCE',1],['REPRODUCE',None],['REPRODUCE',True],{},1,True,None,[['REPRODUCE']]):
            data=copy.deepcopy(original)
            data['flows']['feature']['states']['HUMAN_REVIEW']['resume_to']=invalid
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):simple_config_from_text(json.dumps(data))
        project=simple_config_from_text(self.text)
        compiled=compile_workflow(project.bundle,'feature',self.stages)
        self.assertEqual(compiled.flow.states['HUMAN_REVIEW'].resume_targets,
                         tuple(compiled.entry_states[t] for t in ('REPRODUCE','DIAGNOSE')))
        self.assertEqual(project.show()['flows']['feature']['states']['HUMAN_REVIEW']['resume_targets'],['REPRODUCE','DIAGNOSE'])

    def test_historical_v1_request_and_answer_remain_immutable(self):
        self.states['HUMAN_REVIEW']['resume_to']='REPRODUCE'
        self.states['REPRODUCE']['on']['diagnose']='DIAGNOSE'
        self.text=self.save_config()
        original=WorkWorkspace.write_artifact
        def legacy(store,name,content,*args,**kwargs):
            if name in ('human_request','human_answer'):
                obj=json.loads(content);obj['schema_version']=1
                obj.pop('resume_targets',None);obj.pop('selected_resume_to',None)
                content=json.dumps(obj)
            return original(store,name,content,*args,**kwargs)
        with patch.object(WorkWorkspace,'write_artifact',legacy):self.start()
        q=self.pair()[0][0]
        store=store_for(WorkWorkspace.open(self.work))
        question_bytes=store.read_artifact('human_request')
        self.assertEqual(request_targets(q),('REPRODUCE',))
        with patch.object(WorkWorkspace,'write_artifact',legacy):
            write_answer(WorkWorkspace.open(self.work),q['request_id'],ANSWER)
        answer_bytes=store.read_artifact('human_answer')
        self.assertEqual(resume_v2_work(self.work,self.text,self.config).result.terminal_state,'DONE')
        self.assertEqual(store.read_artifact('human_request'),question_bytes)
        self.assertEqual(store.read_artifact('human_answer'),answer_bytes)
        self.assertIn(ANSWER,self.stages.calls[1].human_clarification[1].content)

    def test_latest_pair_for_same_selected_destination(self):
        self.stages.ask_again=True
        self.start();self.answer('DIAGNOSE','First answer')
        self.answer('DIAGNOSE','Second answer')
        pair=self.stages.calls[2].human_clarification
        self.assertEqual(len(pair),2)
        self.assertIn('Second answer',pair[1].content)
        self.assertNotIn('First answer',pair[1].content)

    def test_sources_follow_selected_destination(self):
        from test_source_resolution import URL, Transport
        transport=Transport()
        with patch('switchyard.github_sources.http_get',transport):
            execute_recoverable_v2_work(self.text,'feature',URL,self.work,self.config)
            self.answer('DIAGNOSE')
        self.assertEqual(len(transport.calls),1)
        self.assertTrue(self.stages.calls[1].sources)
        self.assertFalse(self.stages.calls[2].sources)

    def test_unanswered_v1_request_accepts_new_answer_without_rewriting_request(self):
        self.states['HUMAN_REVIEW']['resume_to']='REPRODUCE'
        self.states['REPRODUCE']['on']['diagnose']='DIAGNOSE'
        self.text=self.save_config()
        original=WorkWorkspace.write_artifact
        def legacy(store,name,content,*args,**kwargs):
            if name=='human_request':
                obj=json.loads(content);obj['schema_version']=1;obj.pop('resume_targets')
                content=json.dumps(obj)
            return original(store,name,content,*args,**kwargs)
        with patch.object(WorkWorkspace,'write_artifact',legacy):self.start()
        store=store_for(WorkWorkspace.open(self.work));before=store.read_artifact('human_request')
        self.answer()
        self.assertEqual(store.read_artifact('human_request'),before)
        self.assertEqual(self.pair()[1][0]['schema_version'],2)
        self.assertEqual(self.pair()[1][0]['selected_resume_to'],'REPRODUCE')

    def test_yaml_and_native_bundle_contract(self):
        import yaml
        from test_workflow_bundle import minimal
        from switchyard.workflow_bundle import load_workflow_bundle
        self.assertEqual(simple_config_from_text(yaml.safe_dump(self.data)).bundle.flow('feature').states['HUMAN_REVIEW'].resume_targets,
                         ('REPRODUCE','DIAGNOSE'))
        data=minimal();states=data['flows']['check']['states']
        states['VERIFY']['on']['pass']='HUMAN_REVIEW'
        states['DIAGNOSE']=dict(role='verify',on={'pass':'DONE'})
        states['HUMAN_REVIEW']=dict(wait_for='human_input',resume_to=['VERIFY','DIAGNOSE'],
                                    budget={'max_state_visits':3,'on_exceeded':'DONE'})
        path=self.root/'bundle.yaml';path.write_text(yaml.safe_dump(data))
        bundle=load_workflow_bundle(path)
        self.assertEqual(bundle.flow('check').states['HUMAN_REVIEW'].resume_targets,('VERIFY','DIAGNOSE'))
        for bad in ([],['VERIFY','VERIFY'],['missing'],['DONE'],['HUMAN_REVIEW'],['VERIFY',False]):
            states['HUMAN_REVIEW']['resume_to']=bad
            path.write_text(yaml.safe_dump(data))
            with self.subTest(bad=bad),self.assertRaises(ValueError):load_workflow_bundle(path)

    def test_control_facts_and_missing_bound_remain_invalid(self):
        for mutation in ('missing_route','missing_bound','budget_to_wait','wait_exhaustion_to_execution'):
            data=copy.deepcopy(self.data);states=data['flows']['feature']['states']
            if mutation=='missing_route': del states['HUMAN_REVIEW']['resume_to']
            elif mutation=='missing_bound': del states['HUMAN_REVIEW']['budget']
            elif mutation=='budget_to_wait': states['REPRODUCE']['budget']['on_exceeded']='HUMAN_REVIEW'
            else: states['HUMAN_REVIEW']['budget']['on_exceeded']='DIAGNOSE'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):simple_config_from_text(json.dumps(data))

    def test_real_process_restart_reconstructs_pending_question_and_selection(self):
        # Each child has a fresh interpreter; only durable files cross the boundary.
        script='''import json, sys
from pathlib import Path
from unittest.mock import patch
from test_human_transition import ReviewStages, ANSWER
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.recovery import execute_recoverable_v2_work, recovery_status, answer_v2_work
work,target,action=sys.argv[1:]
text=(Path(target)/'.switchyard/config.yaml').read_text()
config=ProviderRuntimeConfig(target)
with patch('switchyard.recovery.ProviderStageExecutorResolver',return_value=ReviewStages()):
    if action=='start': execute_recoverable_v2_work(text,'feature','Investigate reported failure',work,config)
    elif action=='answer': answer_v2_work(work,recovery_status(work)['human_input_request_id'],ANSWER,text,config,resume_to='DIAGNOSE')
    print(json.dumps(recovery_status(work)))
'''
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)])}
        def child(action):
            result=subprocess.run([sys.executable,'-c',script,str(self.work),str(self.target),action],env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        first=child('start'); second=child('status')
        self.assertEqual(first,second)
        self.assertEqual(second['question'],QUESTION)
        self.assertEqual(second['resume_targets'],['REPRODUCE','DIAGNOSE'])
        before=self.history()
        final=child('answer')
        self.assertEqual(final['terminal'],'DONE')
        self.assertEqual(final['selected_resume_to'],'DIAGNOSE')
        self.assertEqual(self.history()[:len(before)],before)


class RuntimeHumanTransitionTests(unittest.TestCase):
    class Agent:
        def run(self,request):return InvocationResponse(ExecutorResult('ask'))

    def flow(self):
        return Flow('A',{'A':State('agent',{'ask':'WAIT'}), 'B':State('agent',{'ask':'DONE'}),
                         'WAIT':State(wait_for='human_input',resume_to=('A','B'),max_state_visits_per_run=2,state_visit_budget_exceeded='DONE'),
                         'DONE':State(terminal=True)},10,'DONE')

    def test_runtime_owns_selection_and_events(self):
        flow=self.flow();executors={'agent':self.Agent()};first=run(WorkUnit('test'),flow,executors)
        for target in (None,'DONE','WAIT','unknown'):
            with self.subTest(target=target),self.assertRaisesRegex(ValueError,'declared human continuation'):
                run(WorkUnit('test'),flow,executors,resume_from=first.events,human_input=first.events[-1].state_visit_id,human_resume_to=target)
        second=run(WorkUnit('test'),flow,executors,resume_from=first.events,human_input=first.events[-1].state_visit_id,human_resume_to='B')
        self.assertEqual(second.terminal_state,'DONE')
        self.assertEqual(summarize_runs(second.events)[0].invocation_count,2)
        self.assertEqual([e.target for e in second.events if e.kind=='human_input_resumed'],['B'])

    def test_runtime_rejects_invalid_target_contracts(self):
        from dataclasses import replace
        for targets in ((),('A','A'),('A','DONE'),('A','WAIT'),('unknown',),('A',None)):
            flow=self.flow();states=dict(flow.states);states['WAIT']=replace(states['WAIT'],resume_to=targets)
            with self.subTest(targets=targets),self.assertRaises(FlowValidationError):validate(replace(flow,states=states),{'agent':self.Agent()})

    def test_human_round_limit_is_still_enforced(self):
        flow=self.flow();agents={'agent':self.Agent()};result=run(WorkUnit('test'),flow,agents)
        for _ in range(2):
            result=run(WorkUnit('test'),flow,agents,resume_from=result.events,human_input=result.events[-1].state_visit_id,human_resume_to='A')
        self.assertEqual(result.terminal_state,'DONE')
        self.assertFalse(result.paused)
        self.assertEqual(sum(e.kind=='human_input_requested' for e in result.events),2)
        self.assertEqual(next(e.observed for e in result.events if e.kind=='budget_exceeded'),2)
