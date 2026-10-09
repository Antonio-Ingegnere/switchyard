"""Explicit V2 recovery. No retries: each call requires a caller/human operation.

Metadata uses WorkWorkspace; binary baseline objects are content-addressed within
that workspace. Git-visible files only; ignored data is not recovery-owned.
"""
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from runtime import WorkUnit, run, Event, ExecutionError
from runtime.recording import JsonlRecorder
from .observability import read_events, summarize_runs
from .simple_config import simple_config_from_text, HUMAN_TERMINALS
from .workflow_bundle import ArtifactSpec
from .work_contract import WorkWorkspace
from .workflow_compiler import compile_workflow
from .workflow_execution import WorkflowExecution
from .context_transport import workspace_resolver
from .provider_stages import ProviderStageExecutorResolver
from .provider_process import ProviderExecutionError
from .git_safety import GitRepository
from .qa_lifecycle import withdraw_qa_candidate

CATALOG = {name:ArtifactSpec('json') for name in
           ('baseline','checkpoint','operation','qa_reference','rollback_intent')}
CATALOG['request'] = ArtifactSpec('text',source='request')
CONFIG_PATH = '.switchyard/config.yaml'


class RecoveryBlocked(ValueError):
    def __init__(self, terminal, reason, paths=()):
        self.terminal, self.reason = terminal, reason
        self.paths = tuple(sorted(paths)[:50])
        super().__init__(terminal+': '+reason+(' '+json.dumps(self.paths,ensure_ascii=True)[:2048] if self.paths else ''))


def _json(v): return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n'
def _sha(v): return hashlib.sha256(v).hexdigest()
def _read(store,name): return json.loads(store.read_artifact(name))
def _put(store,name,data): return store.write_artifact(name,_json(data))


def _store(root):
    path=Path(root)/'recovery'
    if not (path/'manifest.jsonl').is_file():
        raise RecoveryBlocked('RECOVERY_BLOCKED','baseline_unavailable_for_historical_work')
    return WorkWorkspace.open(path,CATALOG)


@contextmanager
def _lock(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with (root/'.recovery.lock').open('a') as stream:
        try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryBlocked('RECOVERY_BLOCKED','provider_or_controller_active') from None
        try: yield
        finally: fcntl.flock(stream,fcntl.LOCK_UN)


class _Repo(GitRepository):
    def __init__(self,target,executable=None):
        super().__init__(str(Path(target).resolve()),executable or shutil.which('git'))
        self.env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        self.env['GIT_OPTIONAL_LOCKS']='0'

    def git(self,*args,input=None):
        try:
            return subprocess.run([self.git_executable,'-c','core.filemode=true','--literal-pathspecs',*args],
                cwd=self.workspace,env=self.env,input=input,check=True,capture_output=True,timeout=30).stdout
        except (OSError,subprocess.SubprocessError):
            raise RecoveryBlocked('RECOVERY_BLOCKED','git_inspection_failed') from None

    def identity(self):
        branch,head=self.check_repository()
        return dict(target=str(self.workspace),head=head,branch=branch,
                    git_dir=self.git('rev-parse','--absolute-git-dir').decode().strip())

    def path(self,name):
        if (not name or name.startswith('/') or any(p in ('','.','..','.git') for p in name.split('/'))
            or ('.switchyard' in name.split('/') and name!=CONFIG_PATH)):
            raise RecoveryBlocked('RECOVERY_BLOCKED','unsupported_path',[name])
        path=self.workspace/name
        for parent in path.parents:
            if parent==self.workspace:break
            if parent.is_symlink():raise RecoveryBlocked('RECOVERY_BLOCKED','symlink_parent',[name])
        return path

    def content(self,name):
        path=self.path(name)
        if path.is_symlink():return os.fsencode(os.readlink(path)),'120000'
        if not path.exists():return None,None
        info=path.stat()
        if not stat.S_ISREG(info.st_mode):raise RecoveryBlocked('RECOVERY_BLOCKED','unsupported_file_type',[name])
        return path.read_bytes(),'100755' if info.st_mode & stat.S_IXUSR else '100644'

    def index_path(self):
        p=Path(os.fsdecode(self.git('rev-parse','--git-path','index').strip()))
        return p if p.is_absolute() else self.workspace/p

    def snapshot(self):
        identity=self.identity()
        raw=self.git('ls-files','--cached','--others','--exclude-standard','-z')
        # HEAD includes paths staged for deletion, which ls-files omits.
        tree={}
        for entry in self.git('ls-tree','-r','-z','HEAD').split(b'\0'):
            if entry:
                meta,name=entry.split(b'\t',1);mode,kind,oid=meta.split()
                if kind!=b'blob':raise RecoveryBlocked('RECOVERY_BLOCKED','unsupported_git_entry')
                tree[name.decode('utf-8')]=dict(mode=mode.decode(),oid=oid.decode())
        paths=set(os.fsdecode(p) for p in raw.split(b'\0') if p)|set(tree)
        files={}
        for name in sorted(paths):
            content,mode=self.content(name)
            if content is not None:files[name]=dict(sha256=_sha(content),mode=mode,permissions=stat.S_IMODE(self.path(name).lstat().st_mode))
        index=self.git('ls-files','--stage','-z')
        return dict(identity=identity,files=files,index=_sha(index)),tree


def _blob(store,data):
    digest=_sha(data);directory=store.root/'blobs';directory.mkdir(exist_ok=True)
    path=directory/digest
    if not path.exists():
        with path.open('xb') as out:out.write(data);out.flush();os.fsync(out.fileno())
    if _sha(path.read_bytes())!=digest:raise RecoveryBlocked('RECOVERY_BLOCKED','baseline_blob_corrupt')
    return digest


def _bytes(store,digest):
    if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise RecoveryBlocked('RECOVERY_BLOCKED','invalid_blob_reference')
    data=(store.root/'blobs'/digest).read_bytes()
    if _sha(data)!=digest:raise RecoveryBlocked('RECOVERY_BLOCKED','baseline_blob_corrupt')
    return data


def _baseline(repo,store,config,flow):
    snap,tree=repo.snapshot();sources={}
    for name,fp in snap['files'].items():
        data,mode=repo.content(name)
        entry=tree.get(name)
        # Store only dirty/untracked bytes. Clean tracked bytes remain in base HEAD.
        if entry and entry['mode']==mode and repo.git('cat-file','blob',entry['oid'])==data:
            sources[name]={'git_blob':entry['oid']}
        else:sources[name]={'blob':_blob(store,data)}
    index=repo.index_path().read_bytes()
    if repo.snapshot()[0]!=snap:raise RecoveryBlocked('RECOVERY_BLOCKED','worktree_moved_during_baseline')
    _put(store,'baseline',dict(snapshot=snap,sources=sources,index_blob=_blob(store,index),config=config,flow=flow))
    _put(store,'checkpoint',dict(snapshot=snap,manifest=None))


def _manifest(root):
    p=Path(root)/'manifest.jsonl'
    return _sha(p.read_bytes()) if p.is_file() else None


def _operation(store,phase,**fields):
    value=dict(phase=phase,timestamp=time.time_ns(),**fields)
    _put(store,'operation',value)
    return value


def _checkpoint(store,repo,root):
    snap,_=repo.snapshot()
    _put(store,'checkpoint',dict(snapshot=snap,manifest=_manifest(root)))


def _same_repo(baseline,current,terminal):
    if baseline['identity']!=current['identity']:
        raise RecoveryBlocked(terminal,'base_head_or_repository_changed')


def _drift(expected,current,terminal,paths=None,ignore=()):
    _same_repo(expected,current,terminal)
    if current['index']!=expected['index']:raise RecoveryBlocked(terminal,'index_drift')
    scope=set(paths) if paths is not None else set(expected['files'])|set(current['files'])
    conflicts=[p for p in scope-set(ignore) if expected['files'].get(p)!=current['files'].get(p)]
    if conflicts:raise RecoveryBlocked(terminal,'checkpoint_drift',conflicts)


def _semantic(text):
    from flow_config import _yaml_data
    config=_yaml_data(text)
    for agent in config['agents'].values():agent.pop('timeout',None)
    return config


class _Recorder(JsonlRecorder):
    def __init__(self,path,store,compiled,root):
        super().__init__(path);self.store=store;self.compiled=compiled;self.root=root;self.path=path
    def record(self,event):
        super().record(event)
        with Path(self.path).open('ab') as f:os.fsync(f.fileno())
        if event.kind=='invocation_started':
            binding=self.compiled.plan[event.state].binding
            _operation(self.store,'running',state=binding.logical_state,rung=binding.rung_index,
                       manifest_before=_manifest(self.root))


class _CheckpointExecutor:
    def __init__(self,stage,store,repo,root):self.stage,self.store,self.repo,self.root=stage,store,repo,root
    def run(self,invocation):
        try:return self.stage.run(invocation)
        finally:
            # Adapter transport waits for provider termination before returning/raising.
            op=_read(self.store,'operation')
            if op.get('provider_pid') is None:
                _checkpoint(self.store,self.repo,self.root)


def _compiled(project,flow,workspace,config,store,repo,root):
    provider=ProviderStageExecutorResolver(project.bundle.profiles,config)
    return compile_workflow(project.bundle,flow,workspace_resolver(project.bundle,flow,workspace,
        lambda binding:_CheckpointExecutor(provider(binding),store,repo,root)))


def _drive(project,flow,request,root,config,store,repo,history=None,recover_to=None):
    workspace=WorkWorkspace.open(root,project.bundle.artifacts);workspace.initialize_request(request)
    try:
        def observe(event,pid):
            old=_read(store,'operation')
            old.update(provider_pid=pid if event=='started' else None, process_confirmed_dead=event=='terminated')
            _put(store,'operation',old)
        config=replace(config,process_observer=observe)
        compiled=_compiled(project,flow,workspace,config,store,repo,root)
        result=run(WorkUnit(request),compiled.flow,compiled.executors,
                   recorder=_Recorder(Path(root)/'events.jsonl',store,compiled,root),resume_from=history,recover_to=recover_to)
    except BaseException as exc:
        reason='execution_interrupted';cause=exc
        while cause is not None:
            if isinstance(cause,ProviderExecutionError):
                reason='provider_timeout' if str(cause)=='process timeout' else 'provider_failure';break
            cause=cause.__cause__
        old=_read(store,'operation') if store.versions('operation') else {}
        if old.get('provider_pid') is not None:
            raise  # Cleanup could not prove termination; keep RUNNING and refuse destructive recovery.
        _checkpoint(store,repo,root)
        _operation(store,'interrupted',reason=reason,state=old.get('state'),rung=old.get('rung'),
                   manifest_before=old.get('manifest_before'),diagnostic=str(cause or exc)[:2048],
                   returncode=getattr(cause,'returncode',None),resume_safe=reason.startswith('provider_') and old.get('manifest_before')==_manifest(root))
        raise
    _checkpoint(store,repo,root)
    _operation(store,'delivery_done',terminal=result.terminal_state)
    return WorkflowExecution(result,workspace,compiled)


def execute_recoverable_v2_work(config_text,flow_name,request,work_root,provider_runtime_config):
    """New delivery with a baseline before any provider invocation."""
    root=Path(work_root).resolve()
    project=simple_config_from_text(config_text)
    project.bundle.flow(flow_name)
    with _lock(root):
        if (root/'recovery/manifest.jsonl').exists() or (root/'manifest.jsonl').exists():
            raise RecoveryBlocked('RECOVERY_BLOCKED','workspace_not_fresh')
        store=WorkWorkspace.open(root/'recovery',CATALOG)
        store.initialize_request(request)
        repo=_Repo(provider_runtime_config.working_directory)
        _baseline(repo,store,config_text,flow_name)
        _operation(store,'running')
        return _drive(project,flow_name,request,root,provider_runtime_config,store,repo)


def _capacity(compiled,history,enter=None):
    """Remaining budget for the interrupted visit, or for a new visit of state `enter`."""
    state=compiled.flow.states[enter or history[-1].state]
    inv=[e for e in history if e.kind=='invocation_started']
    if compiled.flow.max_invocations_per_run is not None and len(inv)>=compiled.flow.max_invocations_per_run:return False
    if enter is not None and state.max_state_visits_per_run is not None:
        if sum(e.kind=='state_entered' and e.state==enter for e in history)>=state.max_state_visits_per_run:return False
    for scope in state.invocation_budgets:
        used=sum(any(s.name==scope.name for s in compiled.flow.states[e.state].invocation_budgets) for e in inv)
        if used>=scope.limit:return False
    return True


class _Projection:
    def run(self,request):raise AssertionError('projection only')


def _route(config_text,flow,terminal):
    """The workflow-declared recovery target of a human terminal; never derived from human text."""
    if terminal not in HUMAN_TERMINALS:return None
    spec=simple_config_from_text(config_text).bundle.flow(flow).states.get(terminal)
    return None if spec is None or not spec.terminal else spec.resume_to


def resume_v2_work(work_root,config_text,provider_runtime_config,reason=''):
    """Human-requested continuation of the same run; no budget reset.

    An interrupted visit is re-entered. A human terminal (NEEDS_HUMAN/NEEDS_REDIAGNOSIS)
    continues only into the state its frozen workflow declares with `resume_to`.
    `reason` is bounded recovery metadata and never influences routing.
    """
    if type(reason) is not str or len(reason.encode('utf-8'))>4096:
        raise RecoveryBlocked('RESUME_BLOCKED','reason_too_large')
    root=Path(work_root).resolve()
    with _lock(root):
        store=_store(root);baseline=_read(store,'baseline');op=_read(store,'operation')
        target=None
        if op['phase']=='delivery_done':
            target=_route(baseline['config'],baseline['flow'],op.get('terminal'))
            if target is None or store.versions('qa_reference'):
                raise RecoveryBlocked('RESUME_BLOCKED','no_declared_recovery_route')
        elif op['phase']!='interrupted' or not op.get('resume_safe'):
            raise RecoveryBlocked('RESUME_BLOCKED','not_a_resumable_interruption')
        try:project=simple_config_from_text(config_text)
        except ValueError as exc:
            raise RecoveryBlocked('RESUME_BLOCKED','invalid_configuration: '+str(exc)[:512]) from None
        if _semantic(config_text)!=_semantic(baseline['config']):
            raise RecoveryBlocked('RESUME_BLOCKED','semantic_configuration_changed_new_delivery_required')
        repo=_Repo(provider_runtime_config.working_directory);snap,_=repo.snapshot()
        checkpoint=_read(store,'checkpoint')
        _drift(checkpoint['snapshot'],snap,'RESUME_BLOCKED',ignore=(CONFIG_PATH,))
        history=tuple(read_events(root/'events.jsonl'));summarize_runs(history)
        end=('run_finished',op.get('terminal')) if target else ('run_error',)
        if not history or (history[-1].kind,history[-1].state)[:len(end)]!=end:
            raise RecoveryBlocked('RESUME_BLOCKED','incomplete_event_history')
        compiled=compile_workflow(project.bundle,baseline['flow'],lambda b:_Projection())
        enter=compiled.entry_states[target] if target else None
        if not _capacity(compiled,history,enter):raise RecoveryBlocked('RESUME_BLOCKED','invocation_budget_exhausted')
        workspace=WorkWorkspace.open(root)
        if _manifest(root)!=(checkpoint['manifest'] if target else op['manifest_before']):
            raise RecoveryBlocked('RESUME_BLOCKED','artifact_history_changed')
        if target:
            missing=[a for a in project.bundle.flow(baseline['flow']).states[target].consumes if not workspace.versions(a)]
            if missing:raise RecoveryBlocked('RESUME_BLOCKED','recovery_inputs_missing',missing)
            _operation(store,'running',recovery=dict(from_terminal=op['terminal'],resume_to=target,reason=reason))
        else:
            _operation(store,'running',**({'recovery':dict(reason=reason)} if reason else {}))
        return _drive(project,baseline['flow'],workspace.read_artifact('request'),root,provider_runtime_config,store,repo,history,enter)


def recovery_status(work_root):
    store=_store(work_root);op=_read(store,'operation');base=_read(store,'baseline')
    phase=op['phase']
    if phase=='running':
        try:
            with _lock(work_root):
                pid=op.get('provider_pid')
                alive=False
                if pid is not None:
                    try:os.kill(pid,0);alive=True
                    except ProcessLookupError:pass
                    except PermissionError:alive=True
                if not alive:
                    op=_operation(store,'interrupted',state=op.get('state'),reason='execution_interrupted',
                                  resume_safe=False,checkpoint_uncertain=True)
                    phase='interrupted'
        except RecoveryBlocked:pass  # Another controller still owns the active invocation.
    terminal={'running':'RUNNING','interrupted':'INTERRUPTED','rolled_back':'ROLLED_BACK','rollback_running':'ROLLBACK_BLOCKED'}.get(phase,op.get('terminal'))
    if phase=='delivery_done' and store.versions('qa_reference'):
        ref=_read(store,'qa_reference')
        if not (Path(ref['workspace'])/'manifest.jsonl').is_file():
            raise RecoveryBlocked('RECOVERY_BLOCKED','qa_workspace_unavailable')
        qa=WorkWorkspace.open(ref['workspace'])
        if qa.versions('qa_decision'):terminal=_read(qa,'qa_decision')['terminal']
    resumable=False;route=None
    try:
        if phase=='delivery_done' and not store.versions('qa_reference'):
            route=_route(base['config'],base['flow'],terminal)
        if route or (phase=='interrupted' and op.get('resume_safe')):
            compiled=compile_workflow(simple_config_from_text(base['config']).bundle,base['flow'],lambda b:_Projection())
            resumable=_capacity(compiled,tuple(read_events(Path(work_root)/'events.jsonl')),
                                compiled.entry_states[route] if route else None)
    except Exception:pass
    return dict(work_id=Path(work_root).name,target=base['snapshot']['identity']['target'],flow=base['flow'],phase=phase,terminal=terminal,
                state=op.get('state'),reason=op.get('reason'),diagnostic=op.get('diagnostic'),resume_allowed=resumable,resume_to=route,
                rollback_allowed=phase in ('interrupted','delivery_done','rollback_running') and not op.get('checkpoint_uncertain',False) and terminal!='ACCEPTED')


def attach_qa_candidate(work_root,qa_root):
    """Bind the external QA candidate to its source recovery history."""
    with _lock(work_root):
        store=_store(work_root)
        from .qa_lifecycle import _load
        _,candidate,identity=_load(qa_root)
        if candidate['source_workspace']!=str(Path(work_root).resolve()):raise RecoveryBlocked('RECOVERY_BLOCKED','qa_source_mismatch')
        _put(store,'qa_reference',dict(workspace=str(Path(qa_root).resolve()),identity=identity))


def rollback_v2_work(work_root,reason=''):
    """Restore only checkpoint-owned changes; retain history and finalize QA first."""
    if type(reason) is not str or len(reason.encode('utf-8'))>4096:
        raise RecoveryBlocked('ROLLBACK_BLOCKED','reason_too_large')
    root=Path(work_root).resolve()
    with _lock(root):
        store=_store(root);base=_read(store,'baseline');op=_read(store,'operation')
        if op['phase'] not in ('interrupted','delivery_done','rollback_running'):
            raise RecoveryBlocked('ROLLBACK_BLOCKED','delivery_not_in_rollback_state')
        if store.versions('qa_reference'):
            ref=_read(store,'qa_reference');qa=WorkWorkspace.open(ref['workspace'])
            if qa.versions('qa_decision') and _read(qa,'qa_decision')['terminal']=='ACCEPTED':
                raise RecoveryBlocked('ROLLBACK_BLOCKED','accepted_history_use_git_revert_or_corrective_delivery')
        if op.get('checkpoint_uncertain'):
            raise RecoveryBlocked('ROLLBACK_BLOCKED','uncertain_checkpoint_after_controller_crash')
        baseline=base['snapshot'];repo=_Repo(baseline['identity']['target']);current,_=repo.snapshot()
        checkpoint=_read(store,'checkpoint')['snapshot']
        owned={p for p in set(baseline['files'])|set(checkpoint['files']) if baseline['files'].get(p)!=checkpoint['files'].get(p)}
        _same_repo(baseline,current,'ROLLBACK_BLOCKED')
        if op['phase']=='rollback_running':
            expected=json.loads(_json(checkpoint))
            for p in owned:
                if current['files'].get(p)==baseline['files'].get(p):
                    if p in baseline['files']:expected['files'][p]=baseline['files'][p]
                    else:expected['files'].pop(p,None)
            if current['index']==baseline['index']:expected['index']=baseline['index']
        else:expected=checkpoint
        _drift(expected,current,'ROLLBACK_BLOCKED',paths=owned)
        # Read/verify every restoration blob before any mutation.
        restore={}
        for name in owned:
            if name not in baseline['files']:restore[name]=None;continue
            source=base['sources'][name]
            data=_bytes(store,source['blob']) if 'blob' in source else repo.git('cat-file','blob',source['git_blob'])
            if _sha(data)!=baseline['files'][name]['sha256']:raise RecoveryBlocked('ROLLBACK_BLOCKED','baseline_blob_corrupt',[name])
            restore[name]=data
        index=_bytes(store,base['index_blob'])
        if store.versions('qa_reference'):
            ref=_read(store,'qa_reference');withdraw_qa_candidate(ref['workspace'],ref['identity'])
        _put(store,'rollback_intent',dict(previous_state=op,reason=reason,paths=sorted(owned)))
        _operation(store,'rollback_running')
        # Index lock excludes concurrent cooperating Git writers. Never touch unrelated paths.
        index_path=repo.index_path();lock=Path(str(index_path)+'.lock')
        try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:raise RecoveryBlocked('ROLLBACK_BLOCKED','index_locked') from None
        try:
            _drift(expected,repo.snapshot()[0],'ROLLBACK_BLOCKED',paths=owned)
            for name in sorted(owned,key=lambda n:(-n.count('/'),n)):
                path=repo.path(name)
                if path.is_symlink() or path.is_file():path.unlink()
                elif path.exists():raise RecoveryBlocked('ROLLBACK_BLOCKED','path_type_changed',[name])
            for name,data in restore.items():
                if data is None:continue
                path=repo.path(name);path.parent.mkdir(parents=True,exist_ok=True)
                mode=baseline['files'][name]['mode']
                if mode=='120000':path.symlink_to(os.fsdecode(data))
                else:
                    with path.open('xb') as out:out.write(data);out.flush();os.fsync(out.fileno())
                    path.chmod(baseline['files'][name]['permissions'])
            if current['index']!=baseline['index']:
                with os.fdopen(fd,'wb',closefd=False) as out:out.write(index);out.flush();os.fsync(out.fileno())
                os.replace(lock,index_path)
            final,_=repo.snapshot()
            _same_repo(baseline,final,'ROLLBACK_BLOCKED')
            if final['index']!=baseline['index'] or any(final['files'].get(p)!=baseline['files'].get(p) for p in owned):
                raise RecoveryBlocked('ROLLBACK_BLOCKED','restoration_verification_failed')
        finally:
            owned_index_lock=os.fstat(fd)
            os.close(fd)
            if lock.exists() and lock.stat().st_ino==owned_index_lock.st_ino and lock.stat().st_dev==owned_index_lock.st_dev:
                lock.unlink()
        _operation(store,'rolled_back',terminal='ROLLED_BACK',reason=reason,previous_state=op.get('state',op.get('terminal')),
                   restored_path_count=len(owned))
        return recovery_status(root)
