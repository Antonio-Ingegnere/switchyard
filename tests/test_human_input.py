"""Same-work clarification through real runtime, recovery stores and offline stages."""
from dataclasses import replace
import contextlib
import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from runtime import (WorkUnit, Flow, State, InvocationBudgetScope, InvocationResponse,
                     ExecutorResult, ExecutionError, FlowValidationError, run, validate)
from switchyard.context_transport import StageResult, StageHandoff, ProducedArtifact
from switchyard.human_input import records, store_for, write_answer
from switchyard.recovery import (execute_recoverable_v2_work, answer_v2_work, resume_v2_work,
                                 recovery_status, rollback_v2_work, RecoveryBlocked)
from switchyard.work_contract import WorkWorkspace, WorkContractError
from switchyard.provider_process import ProviderExecutionError
from switchyard.provider_stages import render_invocation
from switchyard.observability import read_events, summarize_runs, ObservabilityError
from switchyard.simple_config import simple_config_from_text
from switchyard.workflow_compiler import compile_workflow
from switchyard.cli import main
from flow_config import FlowConfigError
import test_recovery
from test_source_resolution import Transport, URL

QUESTION = 'Should existing saved query syntax remain backward compatible?'
ANSWER = 'Yes. Existing saved query syntax must remain backward compatible.'
SPEC = json.dumps(dict(id='TQL-001', goal='SQL-inspired query syntax extensions', scope=['typed AST'],
                      constraints=[ANSWER], acceptance=['saved queries work'], verification=['offline tests'], non_goals=[]))


class Stages:
    def __init__(self, rounds=1):
        self.rounds, self.calls, self.spec_calls = rounds, [], 0
        self.fail_next = False
        self.no_handoff = False

    def __call__(self, binding):
        owner = self
        class Stage:
            def run(self, invocation):
                owner.calls.append(invocation)
                if owner.fail_next:
                    owner.fail_next = False
                    raise ProviderExecutionError('provider exited with status 1', returncode=1)
                if binding.logical_state == 'SPECIFY':
                    owner.spec_calls += 1
                    if owner.spec_calls <= owner.rounds:
                        return StageResult('needs_clarification', handoff=None if owner.no_handoff else
                                           StageHandoff('A compatibility decision is needed.', blockers=(QUESTION,)))
                    return StageResult('ready', (ProducedArtifact('microspec', SPEC),), StageHandoff('Scope is grounded.'))
                if binding.logical_state == 'IMPLEMENT':
                    return StageResult('ready', (ProducedArtifact('implementation_handoff', 'implemented'),), StageHandoff('Done.'))
                return StageResult('pass', (ProducedArtifact('verifier_evidence', 'passed'),))
        return Stage()


class HumanInputTests(unittest.TestCase):
    _base = test_recovery.RecoveryTests
    setUp_base, git, save_config = _base.setUp, _base.git, _base.save_config

    def setUp(self):
        self.setUp_base()
        self.states = self.data['flows']['feature']['states']
        self.states['SPECIFY'].update(rung_budget_scope='visit', budget={'visits':4},
                                     on={'ready':'IMPLEMENT', 'needs_clarification':'DECISION'})
        self.states['DECISION'] = dict(wait_for='human_input', resume_to='SPECIFY',
                                      budget={'visits':2, 'on_exceeded':'NEEDS_HUMAN'})
        self.data['flows']['feature']['limits']['run_invocations'] = 8
        self.text = self.save_config()
        self.stages = Stages()
        self.patch = patch('switchyard.recovery.ProviderStageExecutorResolver', return_value=self.stages)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.transport = Transport()
        self.http = patch('switchyard.github_sources.http_get', self.transport)
        self.http.start();self.addCleanup(self.http.stop)

    def start(self):
        return execute_recoverable_v2_work(self.text,'feature',URL,self.work,self.config)

    def answer(self, text=ANSWER, request_id=None):
        request_id = request_id or recovery_status(self.work)['human_input_request_id']
        return answer_v2_work(self.work,request_id,text,self.text,self.config)

    def history(self):
        return tuple(read_events(self.work/'events.jsonl'))

    def record_pair(self):
        return records(WorkWorkspace.open(self.work))

    def test_grounded_request_wait_status_answer_same_run_and_context(self):
        first = self.start()
        self.assertTrue(first.result.paused)
        self.assertEqual(len(self.stages.calls), 1)
        self.assertIn('TQL-001', render_invocation(self.stages.calls[0]))
        status = recovery_status(self.work)
        self.assertEqual((status['state'],status['question'],status['next_action']), ('DECISION',QUESTION,'answer required'))
        self.assertTrue(status['answer_allowed']);self.assertFalse(status['resume_allowed'])
        self.assertIsNone(status['terminal'])
        self.assertFalse(summarize_runs(self.history())[0].completed_normally)
        before = self.history(); questions, answers = self.record_pair()
        question = questions[0]
        self.assertEqual(answers, ())
        finished = next(e for e in before if e.kind=='invocation_finished')
        self.assertEqual(question['origin_invocation_id'], finished.invocation_id)
        self.assertEqual(question['origin_outcome'], 'needs_clarification')
        self.assertEqual(question['origin_rung'], 0)
        self.assertEqual(question['handoff_sequence'], 1)
        with self.assertRaisesRegex(RecoveryBlocked, 'human_input_required') as caught:
            resume_v2_work(self.work,self.text,self.config)
        self.assertEqual(caught.exception.human_input_request['question'], QUESTION)
        self.assertEqual(self.history(), before)
        result = self.answer()
        self.assertEqual(result.result.terminal_state,'READY_FOR_MANUAL_QA')
        self.assertEqual(self.history()[:len(before)], before)
        self.assertEqual({e.run_id for e in self.history()}, {finished.run_id})
        self.assertEqual(recovery_status(self.work)['work_id'], self.work.name)
        self.assertEqual(self.record_pair()[1][0]['answer'], ANSWER)
        second = self.stages.calls[1]
        rendered = render_invocation(second)
        for value in (QUESTION, ANSWER, 'HUMAN CLARIFICATION', 'TQL-001', 'RESOLVED REQUEST SOURCES'):
            self.assertIn(value, rendered)
        self.assertEqual(next(a.content for a in second.artifacts if a.record.name=='request'), URL)
        self.assertEqual(len(self.transport.calls),1)
        self.assertFalse(self.stages.calls[2].human_clarification)
        events = self.history()
        self.assertEqual(sum(e.kind=='human_input_received' for e in events),1)
        self.assertEqual(sum(e.kind=='human_input_resumed' for e in events),1)
        specifies = [e for e in events if e.kind=='invocation_started' and 'SPECIFY' in e.state]
        self.assertEqual(len({e.invocation_id for e in specifies}),2)
        self.assertEqual(len({e.state_visit_id for e in specifies}),2)
        self.assertTrue(summarize_runs(events)[0].completed_normally)
        with self.assertRaises(RecoveryBlocked):
            self.answer(request_id=question['request_id'])

    def test_reject_empty_invalid_utf8_oversized_stale_answers_without_writes(self):
        self.start()
        before = self.history()
        for answer in ('', ' \n', '\ud800', 'x'*8193, 'é'*4097):
            with self.subTest(size=len(answer)), self.assertRaises(WorkContractError):
                self.answer(answer)
        with self.assertRaisesRegex(RecoveryBlocked,'stale'):
            self.answer(request_id='old-request')
        self.assertEqual(self.record_pair()[1],())
        self.assertEqual(self.history(),before)
        self.assertEqual(len(self.stages.calls),1)

    def test_two_rounds_then_ready_preserve_all_records(self):
        self.stages.rounds = 2
        self.start(); first = recovery_status(self.work)['human_input_request_id']
        self.answer('First answer')
        second = recovery_status(self.work)
        self.assertEqual(second['human_input_round'],2)
        self.assertNotEqual(second['human_input_request_id'], first)
        with self.assertRaisesRegex(RecoveryBlocked,'stale'):
            self.answer(request_id=first)
        self.assertEqual(self.answer('Second answer').result.terminal_state,'READY_FOR_MANUAL_QA')
        questions,answers = self.record_pair()
        self.assertEqual([q['round'] for q in questions],[1,2])
        self.assertEqual([a['answer'] for a in answers],['First answer','Second answer'])
        context = render_invocation(self.stages.calls[2])
        self.assertIn('Second answer',context);self.assertNotIn('First answer',context)
        self.assertEqual(len(self.transport.calls),1)

    def test_clarification_bound_stops_explicitly(self):
        self.stages.rounds = 100
        self.start();self.answer('first'); result = self.answer('second')
        self.assertFalse(result.result.paused)
        self.assertEqual(result.result.terminal_state,'NEEDS_HUMAN')
        refusal = next(e for e in result.result.events if e.kind=='budget_exceeded')
        self.assertEqual((refusal.state,refusal.metric,refusal.limit,refusal.observed),
                         ('DECISION','max_state_visits_per_run',2,2))
        self.assertEqual(recovery_status(self.work)['reason'],'human_input_round_limit')
        self.assertEqual(len(self.record_pair()[0]),2)
        self.assertEqual(len(self.stages.calls),3)

    def test_provider_failure_after_answer_preserves_answer_and_budget_counts(self):
        self.start(); request_id = recovery_status(self.work)['human_input_request_id']
        self.stages.fail_next = True
        with self.assertRaises(ExecutionError):self.answer()
        status = recovery_status(self.work)
        self.assertTrue(status['resume_allowed'])
        self.assertEqual(status['infrastructure_failures_used'],1)
        run_budget = next(b for b in status['semantic_budgets'] if b['scope']=='run')
        self.assertEqual(run_budget['used'],1)  # The valid clarification is still charged.
        self.assertEqual(status['semantic_invocations_used'],0)  # Current visit's rung had no semantic result.
        self.assertEqual(self.record_pair()[1][0]['answer'],ANSWER)
        with self.assertRaises(RecoveryBlocked):self.answer(request_id=request_id)
        result = resume_v2_work(self.work,self.text,self.config)
        self.assertEqual(result.result.terminal_state,'READY_FOR_MANUAL_QA')
        self.assertIn(ANSWER,render_invocation(self.stages.calls[2]))
        specifies = [e for e in self.history() if e.kind=='invocation_started' and 'SPECIFY' in e.state]
        self.assertEqual(len({e.invocation_id for e in specifies}),3)
        self.assertEqual(specifies[1].state_visit_id,specifies[2].state_visit_id)
        self.assertNotEqual(specifies[0].state_visit_id,specifies[1].state_visit_id)
        self.assertEqual(len(self.record_pair()[1]),1)

    def test_run_budget_not_reset_by_human_answer(self):
        self.data['flows']['feature']['limits']['run_invocations']=1
        self.text=self.save_config();self.start()
        status=recovery_status(self.work)
        self.assertFalse(status['answer_allowed'])
        self.assertEqual(status['resume_blocked'],'invocation_budget_exhausted')
        with self.assertRaisesRegex(RecoveryBlocked,'invocation_budget_exhausted'):self.answer()
        self.assertEqual(len(self.stages.calls),1)

    def test_cumulative_state_budget_remains_distinct_from_per_visit_rung(self):
        self.states['SPECIFY']['budget']['invocations']=1
        self.text=self.save_config();self.start()
        with self.assertRaisesRegex(RecoveryBlocked,'invocation_budget_exhausted'):self.answer()
        self.assertEqual(len(self.stages.calls),1)

    def test_answer_published_before_run_can_restart_without_reanswering(self):
        self.start()
        workspace=WorkWorkspace.open(self.work)
        question=self.record_pair()[0][-1]
        write_answer(workspace,question['request_id'],ANSWER)  # Simulated stop after durable publication.
        status=recovery_status(self.work)
        self.assertFalse(status['human_input_pending']);self.assertTrue(status['resume_allowed'])
        with self.assertRaisesRegex(RecoveryBlocked,'already_answered'):self.answer(request_id=question['request_id'])
        self.assertEqual(resume_v2_work(self.work,self.text,self.config).result.terminal_state,'READY_FOR_MANUAL_QA')

    def test_restart_reconstructs_wait_before_operation_publication(self):
        self.start()
        from switchyard.recovery import _store, _operation
        _operation(_store(self.work),'running')  # Crash after committed request/event/checkpoint.
        status=recovery_status(self.work)
        self.assertEqual(status['question'],QUESTION)
        self.assertTrue(status['answer_allowed'])
        self.answer()

    def test_rollback_while_waiting_retains_question_history(self):
        self.start(); before=self.history()
        status=rollback_v2_work(self.work)
        self.assertEqual(status['terminal'],'ROLLED_BACK')
        self.assertEqual(self.history(),before)
        self.assertEqual(self.record_pair()[0][0]['question'],QUESTION)
        with self.assertRaises(RecoveryBlocked):self.answer(request_id=self.record_pair()[0][0]['request_id'])

    def test_legacy_needs_human_is_not_a_wait(self):
        del self.states['DECISION']
        self.states['SPECIFY']['on']['needs_clarification']='NEEDS_HUMAN'
        self.text=self.save_config(); result=self.start()
        self.assertFalse(result.result.paused)
        self.assertFalse(recovery_status(self.work)['resume_allowed'])
        self.assertFalse((self.work/'human_input').exists())
        self.assertEqual(self.record_pair(),((),()))

    def test_missing_handoff_is_not_replaced_by_fabricated_question(self):
        self.stages.no_handoff=True
        with self.assertRaises(ExecutionError):self.start()
        self.assertEqual(self.record_pair(),((),()))

    def test_status_cli_exposes_question_without_provider_call(self):
        self.start()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['work','status',str(self.work)]),0)
        self.assertEqual(json.loads(output.getvalue())['question'],QUESTION)
        self.assertEqual(len(self.stages.calls),1)

    def test_answer_cli_uses_fixed_target_and_same_work(self):
        self.start();request_id=recovery_status(self.work)['human_input_request_id']
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['work','answer',str(self.work),request_id,ANSWER]),0)
        self.assertEqual(json.loads(output.getvalue())['work_id'],self.work.name)
        self.assertIn(ANSWER,render_invocation(self.stages.calls[1]))

    def test_claude_and_codex_bindings_use_same_contract(self):
        self.data['agents']['spec']['provider']='claude'
        self.data['agents']['spec']['model']='sonnet'
        self.text=self.save_config();self.start();self.answer()
        self.assertEqual(self.stages.calls[0].binding.provider,'claude')
        self.assertIn(ANSWER,render_invocation(self.stages.calls[1]))

    def test_real_fake_cli_claude_and_codex_receive_the_answer(self):
        self.patch.stop()
        script=(Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        adjustment = """if state == 'SPECIFY':
    if 'HUMAN CLARIFICATION' not in text:
        result['outcome'] = 'needs_clarification'
        result['produced_artifacts'] = []
        result['handoff']['blockers'] = [QUESTION_TEXT]
    else:
        assert ANSWER_TEXT in text
        assert 'TQL-001' in text
        assert 'RESOLVED REQUEST SOURCES' in text
""".replace('QUESTION_TEXT',repr(QUESTION)).replace('ANSWER_TEXT',repr(ANSWER))
        script=script.replace('raw=json.dumps(result)',adjustment+'raw=json.dumps(result)',1)
        for name in ('claude','codex'):
            (self.root/name).write_text('#!'+sys.executable+'\n'+script)
        self.file.write_text('finished')
        for provider,model in [('codex','offline'),('claude','sonnet')]:
            with self.subTest(provider=provider):
                self.work=self.root/('work-'+provider)
                self.data['agents']['spec'].update(provider=provider,model=model)
                self.text=self.save_config()
                self.assertTrue(self.start().result.paused)
                self.assertEqual(self.answer().result.terminal_state,'READY_FOR_MANUAL_QA')
        logged=[json.loads(line) for line in self.log.read_text().splitlines()]
        specify=[row for row in logged if row['binding']['logical_state']=='SPECIFY']
        self.assertEqual([row['provider'] for row in specify],['codex','codex','claude','claude'])
        self.assertTrue(all(ANSWER in row['input'] for row in specify[1::2]))

    def test_infrastructure_failures_are_cumulative_across_human_rounds(self):
        self.stages.rounds=2
        self.start();self.stages.fail_next=True
        with self.assertRaises(ExecutionError):self.answer('first')
        resume_v2_work(self.work,self.text,self.config)
        status=recovery_status(self.work)
        self.assertEqual(status['human_input_round'],2)
        self.assertEqual(status['infrastructure_failures_used'],1)
        self.stages.fail_next=True
        with self.assertRaises(ExecutionError):self.answer('second')
        status=recovery_status(self.work)
        self.assertEqual(status['infrastructure_failures_used'],2)
        self.assertFalse(status['resume_allowed'])
        self.assertEqual(status['resume_blocked'],'infrastructure_budget_exhausted')
        self.assertTrue(status['human_input_answered'])
        self.assertEqual(next(b['used'] for b in status['semantic_budgets'] if b['scope']=='run'),2)

    def test_configuration_rejects_missing_bound_or_arbitrary_target(self):
        for replacement in (dict(wait_for='human_input',resume_to='SPECIFY'),
                            dict(wait_for='human_input',resume_to='unknown',budget={'visits':2,'on_exceeded':'NEEDS_HUMAN'}),
                            dict(wait_for='human_input',resume_to='DECISION',budget={'visits':2,'on_exceeded':'NEEDS_HUMAN'})):
            self.states['DECISION']=replacement
            with self.assertRaises((FlowConfigError,ValueError)):simple_config_from_text(json.dumps(self.data))


class RuntimeHumanTests(unittest.TestCase):
    def flow(self, scope='visit', run_limit=4):
        return Flow('spec', {'spec':State('agent',{'ask':'decision','ready':'done'},max_state_visits_per_run=3,
                              invocation_budgets=(InvocationBudgetScope('rung',1,'done','spec' if scope=='visit' else None),),
                              state_visit_budget_exceeded='done'),
                            'decision':State(wait_for='human_input',resume_to='spec',max_state_visits_per_run=2,
                                             state_visit_budget_exceeded='done'), 'done':State(terminal=True)},run_limit,'done')

    def test_per_visit_scope_and_semantic_charges(self):
        class Agent:
            calls=0
            def run(self,request):
                self.calls+=1
                return InvocationResponse(ExecutorResult('ask' if self.calls==1 else 'ready'))
        for scope, expected in [('run',1),('visit',2)]:
            agent=Agent();flow=self.flow(scope)
            first=run(WorkUnit('request'),flow,{'agent':agent})
            with self.assertRaises(ValueError):run(WorkUnit('request'),flow,{'agent':agent},resume_from=first.events)
            with self.assertRaises(ValueError):run(WorkUnit('request'),flow,{'agent':agent},resume_from=first.events,human_input='wrong')
            second=run(WorkUnit('request'),flow,{'agent':agent},resume_from=first.events,human_input=first.events[-1].state_visit_id)
            self.assertEqual(agent.calls,expected)
            self.assertEqual(summarize_runs(second.events)[0].invocation_count,expected)
            self.assertEqual(second.events[:len(first.events)],first.events)

    def test_global_budget_still_charges_clarification(self):
        class Agent:
            def run(self,request):return InvocationResponse(ExecutorResult('ask'))
        flow=self.flow(run_limit=1);agents={'agent':Agent()}
        first=run(WorkUnit('request'),flow,agents)
        result=run(WorkUnit('request'),flow,agents,resume_from=first.events,human_input=first.events[-1].state_visit_id)
        self.assertEqual(summarize_runs(result.events)[0].invocation_count,1)
        self.assertEqual(next(e.metric for e in result.events if e.kind=='budget_exceeded'),'max_invocations_per_run')
