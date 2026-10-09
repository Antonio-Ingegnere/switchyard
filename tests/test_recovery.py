"""Real processes, real Git, durable recovery; no live providers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import ExecutionError
from switchyard.simple_config import simple_config_from_text
from flow_config import FlowConfigError
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.recovery import (execute_recoverable_v2_work,resume_v2_work,rollback_v2_work,
                                 recovery_status,attach_qa_candidate,RecoveryBlocked,_Repo,_lock)
from switchyard.work_contract import WorkWorkspace
from switchyard.observability import read_events,summarize_runs
from switchyard.qa_lifecycle import create_qa_candidate,accept_v2_work,reopen_v2_work,LifecycleError

ROOT=Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.target=self.root/'target';self.target.mkdir()
        self.work=self.root/'work';self.qa=self.root/'qa'
        self.file=self.target/'source.py';self.file.write_text('before')
        (self.target/'remove.txt').write_text('restore me')
        (self.target/'.gitignore').write_text('.switchyard/*\n!.switchyard/config.yaml\n__pycache__/\n')
        (self.target/'.switchyard').mkdir()
        self.data={'agents':{
            'spec':dict(provider='codex',model='offline',workspace='read',shell=True,timeout=2),
            'code':dict(provider='claude',model='sonnet',workspace='write',shell=True,timeout=1),
            'tests':dict(provider='command',command=[sys.executable,'-c','from pathlib import Path; assert Path("source.py").read_text()=="finished"'],capture='verifier_evidence',timeout=2)},
            'flows':{'feature':{'start':'SPECIFY','limits':{'run_invocations':5},'states':{
                'SPECIFY':dict(agent='spec',consumes=['request'],produces={'microspec':dict(format='yaml',schema='microspec')},on={'ready':'IMPLEMENT'}),
                'IMPLEMENT':dict(run=[dict(agent='code',budget={'invocations':2})],consumes=['microspec'],produces={'implementation_handoff':dict(format='markdown')},on={'ready':'VERIFY'}),
                'VERIFY':dict(agent='tests',consumes=['implementation_handoff'],produces={'verifier_evidence':dict(format='markdown')},on={'pass':'READY_FOR_MANUAL_QA','fail':'NEEDS_HUMAN'})}}}}
        self.configfile=self.target/'.switchyard/config.yaml';self.text=self.save_config()
        for args in [('init','-b','main'),('config','user.name','Recovery Tests'),('config','user.email','tests@example.invalid'),('config','commit.gpgsign','false'),('add','--','.gitignore','.switchyard/config.yaml','source.py','remove.txt'),('commit','-m','baseline')]:self.git(*args)
        self.head=self.git('rev-parse','HEAD')
        self.plan=self.root/'plan';self.plan.write_text('{"artifacts":{}}')
        self.log=self.root/'calls'
        script=(ROOT/'tests/fixtures/provider_cli.py').read_text()
        mutation='''if state=='IMPLEMENT':
    Path(os.environ['PID_FILE']).write_text(str(os.getpid()))
    source=Path('source.py')
    if source.read_text()=='before':
        source.write_text('partial')
        Path('remove.txt').unlink()
        Path('created.bin').write_bytes(b'\\x00new\\xff')
        time.sleep(3)
    else:
        assert source.read_text()=='partial', 'partial work must survive resume'
        time.sleep(1.1)
        source.write_text('finished')
'''
        script=script.replace('outcome=plan.get',mutation+'outcome=plan.get')
        for name in ('claude','codex'):
            exe=self.root/name;exe.write_text('#!'+sys.executable+'\n'+script);exe.chmod(0o755)
        self.config=ProviderRuntimeConfig(str(self.target),str(self.root/'claude'),str(self.root/'codex'),
            environment={**os.environ,'FAKE_PLAN':str(self.plan),'FAKE_LOG':str(self.log),'FAKE_CONTRACT_AWARE':'1','PID_FILE':str(self.root/'pid')})

    def git(self,*args):return subprocess.check_output(['git',*args],cwd=self.target,stderr=subprocess.DEVNULL).decode().strip()
    def save_config(self):
        text=json.dumps(self.data);self.configfile.write_text(text);return text
    def start(self):return execute_recoverable_v2_work(self.text,'feature','Human task',self.work,self.config)
    def interrupt(self):
        with self.assertRaises(ExecutionError):self.start()
        self.assertEqual(recovery_status(self.work)['terminal'],'INTERRUPTED')
    def resume(self):
        self.data['agents']['code']['timeout']=5
        return resume_v2_work(self.work,self.save_config(),self.config)
    def ready(self):
        self.interrupt();execution=self.resume()
        candidate=create_qa_candidate(self.target,execution.workspace,execution.result,'feature',self.qa)
        attach_qa_candidate(self.work,self.qa)
        return execution,candidate

    def test_timeout_real_process_dead_partial_checkpoint_and_no_false_output(self):
        self.interrupt();status=recovery_status(self.work)
        self.assertEqual((status['state'],status['reason']),('IMPLEMENT','provider_timeout'))
        self.assertTrue(status['resume_allowed']);self.assertTrue(status['rollback_allowed'])
        with self.assertRaises(ProcessLookupError):os.kill(int((self.root/'pid').read_text()),0)
        self.assertEqual(self.file.read_text(),'partial')
        work=WorkWorkspace.open(self.work)
        self.assertEqual(len(work.versions('microspec')),1)
        self.assertEqual(len(work.versions('implementation_handoff')),0)
        self.assertEqual(len(work.handoffs()),1)
        store=WorkWorkspace.open(self.work/'recovery');self.assertGreater(len(store.versions('checkpoint')),1)

    def test_resume_same_identity_history_budgets_context_and_retuned_timeout(self):
        self.interrupt();workspace=WorkWorkspace.open(self.work);spec=workspace.read_artifact('microspec')
        before=tuple(read_events(self.work/'events.jsonl'));execution=self.resume()
        self.assertEqual(execution.result.terminal_state,'READY_FOR_MANUAL_QA')
        events=tuple(read_events(self.work/'events.jsonl'))
        self.assertEqual(events[:len(before)],before)
        self.assertEqual(len({e.run_id for e in events}),1)
        self.assertEqual(len([e for e in events if e.kind=='invocation_started']),4)
        self.assertEqual(len([e for e in events if e.kind=='state_entered' and 'IMPLEMENT' in e.state]),1)
        self.assertEqual(workspace.read_artifact('microspec'),spec);self.assertEqual(len(workspace.versions('microspec')),1)
        calls=[json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([c['binding']['logical_state'] for c in calls],['SPECIFY','IMPLEMENT','IMPLEMENT'])
        inputs=json.loads(calls[-1]['input'].split('DURABLE INPUT ARTIFACTS\n')[1].split('\n\n')[0])
        self.assertEqual(inputs[0]['content'],spec)
        self.assertEqual(self.file.read_text(),'finished')
        self.assertTrue(summarize_runs(events)[0].completed_normally)

    def test_exhausted_budget_does_not_reset(self):
        self.data['flows']['feature']['states']['IMPLEMENT']['run'][0]['budget']['invocations']=1
        self.text=self.save_config();self.interrupt();before=self.log.read_bytes()
        with self.assertRaisesRegex(RecoveryBlocked,'invocation_budget_exhausted'):self.resume()
        self.assertEqual(self.log.read_bytes(),before)
        self.assertFalse(recovery_status(self.work)['resume_allowed'])

    def test_semantic_mutations_block_before_execution(self):
        self.interrupt();original=json.loads(json.dumps(self.data));before=self.log.read_bytes()
        for change in (lambda d:d['agents']['code'].update(model='haiku'),
                       lambda d:d['agents']['code'].update(workspace='read'),
                       lambda d:d['flows']['feature']['states']['IMPLEMENT']['on'].update(ready='NEEDS_HUMAN')):
            self.data=json.loads(json.dumps(original));change(self.data)
            with self.assertRaises((RecoveryBlocked,FlowConfigError)):resume_v2_work(self.work,self.save_config(),self.config)
        self.assertEqual(self.log.read_bytes(),before)

    def test_drift_blocks_resume_and_rollback(self):
        self.interrupt();self.file.write_text('human edit')
        for op in (lambda:self.resume(),lambda:rollback_v2_work(self.work)):
            with self.assertRaisesRegex(RecoveryBlocked,'checkpoint_drift.*source.py'):op()
        self.assertEqual(self.file.read_text(),'human edit');self.assertEqual(self.git('rev-parse','HEAD'),self.head)

    def test_clean_rollback_all_path_kinds_and_history(self):
        before=_Repo(self.target).snapshot()[0];self.interrupt();result=rollback_v2_work(self.work,'Wrong direction')
        self.assertEqual(result['terminal'],'ROLLED_BACK')
        self.assertEqual(_Repo(self.target).snapshot()[0],before)
        self.assertTrue((self.work/'manifest.jsonl').is_file());self.assertTrue((self.work/'recovery/manifest.jsonl').is_file())
        self.assertFalse((self.target/'created.bin').exists())

    def test_dirty_baseline_binary_symlink_staging_and_overlap(self):
        (self.target/'dirty.bin').write_bytes(b'\x00before\xff');self.git('add','dirty.bin');self.git('commit','-m','track binary')
        (self.target/'dirty.bin').write_bytes(b'\x00user\xfe');(self.target/'dirty.bin').chmod(0o600);self.git('add','dirty.bin')
        (self.target/'untracked.bin').write_bytes(b'\x80user\x00')
        (self.target/'link').symlink_to('source.py')
        (self.target/'remove.txt').unlink()
        before=_Repo(self.target).snapshot()[0]
        # A write-capable fake provider edits all dirty/untracked kinds, then fails.
        path=self.root/'claude';script=path.read_text();a=script.index("if state=='IMPLEMENT':");b=script.index('outcome=plan.get',a)
        script=script[:a]+'''if state=='IMPLEMENT':
    Path('dirty.bin').write_bytes(b'agent')
    Path('dirty.bin').chmod(0o755)
    Path('untracked.bin').unlink()
    Path('link').unlink()
    Path('link').symlink_to('remove.txt')
    Path('source.py').write_text('partial')
    Path('created.bin').write_bytes(b'new')
    time.sleep(3)
'''+script[b:];path.write_text(script)
        self.interrupt();rollback_v2_work(self.work)
        self.assertEqual(_Repo(self.target).snapshot()[0],before)
        self.assertEqual((self.target/'dirty.bin').read_bytes(),b'\x00user\xfe')
        self.assertEqual((self.target/'untracked.bin').read_bytes(),b'\x80user\x00')
        self.assertEqual(os.readlink(self.target/'link'),'source.py')

    def test_qa_rollback_withdraws_candidate_and_retains_history(self):
        execution,candidate=self.ready();rollback_v2_work(self.work)
        self.assertEqual(self.file.read_text(),'before')
        with self.assertRaisesRegex(LifecycleError,'finalized'):accept_v2_work(self.target,self.qa)
        self.assertEqual(WorkWorkspace.open(self.qa).latest_artifact('qa_candidate').sha256,candidate.identity)
        self.assertEqual(recovery_status(self.work)['terminal'],'ROLLED_BACK')

    def test_accepted_refuses_without_history_change(self):
        self.ready();self.assertEqual(accept_v2_work(self.target,self.qa).terminal,'ACCEPTED')
        head=self.git('rev-parse','HEAD')
        with self.assertRaisesRegex(RecoveryBlocked,'accepted_history_use_git_revert'):rollback_v2_work(self.work)
        self.assertEqual(self.git('rev-parse','HEAD'),head)

    def test_reopened_can_rollback(self):
        self.ready();reopen_v2_work(self.target,self.qa,'Needs more work')
        self.assertEqual(rollback_v2_work(self.work)['terminal'],'ROLLED_BACK')
        self.assertEqual(WorkWorkspace.open(self.qa).read_artifact('qa_feedback'),'Needs more work')

    def test_unrelated_post_interruption_changes_are_not_deleted_by_rollback(self):
        self.interrupt();(self.target/'human-new.txt').write_text('preserve')
        rollback_v2_work(self.work)
        self.assertEqual((self.target/'human-new.txt').read_text(),'preserve')

    def test_active_controller_refuses(self):
        self.interrupt()
        with _lock(self.work):
            with self.assertRaisesRegex(RecoveryBlocked,'active'):rollback_v2_work(self.work)

    def test_baseline_head_drift_blocks(self):
        self.interrupt();self.git('commit','--allow-empty','-m','independent')
        with self.assertRaisesRegex(RecoveryBlocked,'base_head_or_repository_changed'):rollback_v2_work(self.work)

    def test_agent_timeouts_reach_two_actual_processes(self):
        exe=self.root/'codex';exe.write_text(exe.read_text().replace('outcome=plan.get',"if state=='SPECIFY': time.sleep(1.1)\noutcome=plan.get"))
        self.interrupt()
        self.assertEqual(len(WorkWorkspace.open(self.work).versions('microspec')),1)
        self.assertEqual(self.file.read_text(),'partial')

    def test_provider_failure_checkpoints_and_classifies(self):
        exe=self.root/'claude';exe.write_text(exe.read_text().replace('time.sleep(3)','raise SystemExit(7)'))
        self.interrupt()
        self.assertEqual(recovery_status(self.work)['reason'],'provider_failure')
        self.assertEqual(self.file.read_text(),'partial')
        op=json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('operation'))
        self.assertEqual(op['returncode'],7)

    def test_run_budget_not_reset_on_resume(self):
        self.data['flows']['feature']['limits']['run_invocations']=2
        self.text=self.save_config();self.interrupt()
        with self.assertRaisesRegex(RecoveryBlocked,'invocation_budget_exhausted'):self.resume()

    def test_state_budget_not_reset_on_resume(self):
        self.data['flows']['feature']['states']['IMPLEMENT']['budget']={'invocations':1}
        self.text=self.save_config();self.interrupt()
        with self.assertRaisesRegex(RecoveryBlocked,'invocation_budget_exhausted'):self.resume()

    def test_commit_blocked_can_rollback_preserving_unrelated_work(self):
        self.ready();(self.target/'human-extra.txt').write_text('keep')
        self.assertEqual(accept_v2_work(self.target,self.qa).terminal,'COMMIT_BLOCKED')
        self.assertEqual(rollback_v2_work(self.work)['terminal'],'ROLLED_BACK')
        self.assertEqual((self.target/'human-extra.txt').read_text(),'keep')

    def test_real_active_provider_refuses_rollback(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.start)
            until=time.monotonic()+10
            while not (self.root/'pid').exists() and time.monotonic()<until:time.sleep(.02)
            self.assertTrue((self.root/'pid').exists())
            self.assertEqual(recovery_status(self.work)['terminal'],'RUNNING')
            with self.assertRaisesRegex(RecoveryBlocked,'active'):rollback_v2_work(self.work)
            with self.assertRaises(ExecutionError):future.result()
        self.assertEqual(recovery_status(self.work)['terminal'],'INTERRUPTED')

    def test_dead_controller_does_not_masquerade_as_running(self):
        self.interrupt();store=WorkWorkspace.open(self.work/'recovery')
        old=json.loads(store.read_artifact('operation'));old.update(phase='running',provider_pid=99999999)
        store.write_artifact('operation',json.dumps(old))
        status=recovery_status(self.work)
        self.assertEqual((status['terminal'],status['reason']),('INTERRUPTED','execution_interrupted'))
        self.assertFalse(status['resume_allowed']);self.assertFalse(status['rollback_allowed'])

    @unittest.skipUnless(os.name=='posix','POSIX process-group cleanup')
    def test_timeout_stops_tool_child_before_checkpoint(self):
        import time
        marker=self.root/'child-wrote'
        child="import time;from pathlib import Path;time.sleep(2);Path("+repr(str(marker))+").write_text('unsafe')"
        exe=self.root/'claude';text=exe.read_text()
        text=text.replace("        time.sleep(3)","        import subprocess\n        subprocess.Popen([sys.executable,'-c',"+repr(child)+"])\n        time.sleep(3)")
        exe.write_text(text);self.interrupt();time.sleep(1.3)
        self.assertFalse(marker.exists())

    def test_agent_staging_restores_original_dirty_index(self):
        self.file.write_text('user staged')
        self.git('add','--','source.py')
        self.file.write_text('before')
        baseline_index=self.git('diff','--cached','--binary')
        exe=self.root/'claude';text=exe.read_text().replace("        time.sleep(3)","        import subprocess\n        subprocess.run(['git','add','--','source.py','remove.txt','created.bin'],check=True)\n        time.sleep(3)")
        exe.write_text(text);self.interrupt()
        self.assertNotEqual(self.git('diff','--cached','--binary'),baseline_index)
        rollback_v2_work(self.work)
        self.assertEqual(self.git('diff','--cached','--binary'),baseline_index)
        self.assertEqual(self.file.read_text(),'before')

    def test_timeout_validation_and_show(self):
        for value in (0,-1,True,'600',float('nan'),float('inf'),None):
            with self.subTest(value=value):
                self.data['agents']['code']['timeout']=value
                with self.assertRaisesRegex(FlowConfigError,'agents.code.timeout'):simple_config_from_text(json.dumps(self.data))
        self.data['agents']['code']['timeout']=1800
        config=simple_config_from_text(json.dumps(self.data));rows={r['agent']:r for r in config.show()['invocations']}
        self.assertEqual(rows['spec']['timeout'],2);self.assertEqual(rows['code']['timeout'],1800)
        self.assertEqual(config.bundle.profiles['code'].timeout_seconds,1800)


if __name__=='__main__':unittest.main()
