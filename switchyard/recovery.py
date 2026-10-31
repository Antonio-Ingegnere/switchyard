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
import re
import shutil
import stat
import subprocess
import time
import uuid

from runtime import WorkUnit, run, Event, ExecutionError, RunResult
from runtime.recording import JsonlRecorder
from .observability import read_events, summarize_runs
from .simple_config import simple_config_from_text
from .workflow_bundle import ArtifactSpec
from .work_contract import WorkWorkspace
from .workflow_compiler import compile_workflow
from .workflow_execution import WorkflowExecution
from .context_transport import workspace_resolver
from .human_input import HumanInputRecorder, active_request, write_answer, validate_answer, request_targets, select_target, answer_target
from .provider_stages import ProviderStageExecutorResolver
from .provider_process import ProviderExecutionError
from .context_transport import EvidenceDeliveryError
from .git_safety import GitRepository
from .qa_lifecycle import (withdraw_qa_candidate, finish_reopen_v2_work, accept_v2_work, finish_accept_v2_work, create_qa_candidate,
                           QaDecision,
                           fix_records, write_fix_request, resolve_fix_request, qa_rounds,
                           finding_inputs, evidence_plan, load_evidence, request_findings, finding_states,
                           revalidate_candidate, _load as _load_candidate, LifecycleError)

CATALOG = {name:ArtifactSpec('json') for name in
           ('baseline','checkpoint','operation','qa_reference','rollback_intent')}
CATALOG['request'] = ArtifactSpec('text',source='request')
CONFIG_PATH = '.switchyard/config.yaml'
QA_READY = 'READY_FOR_MANUAL_QA'
# Durable marker: the corrective run finished at QA but its new candidate is not yet current.
PUBLICATION_PENDING = 'candidate_publication_pending'


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

    def probe(self,*args):
        """(returncode, stdout) for questions Git answers with an exit status; never raises on nonzero."""
        try:
            result=subprocess.run([self.git_executable,'-c','core.filemode=true','--literal-pathspecs',*args],
                cwd=self.workspace,env=self.env,capture_output=True,timeout=30)
        except (OSError,subprocess.SubprocessError):
            raise RecoveryBlocked('RECOVERY_BLOCKED','git_inspection_failed') from None
        return result.returncode,result.stdout

    def common_dir(self):
        p=Path(os.fsdecode(self.git('rev-parse','--git-common-dir').strip()))
        return str((p if p.is_absolute() else self.workspace/p).resolve())

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


# --- Delivery isolation (#3): one branch + one Git worktree per work -------------------------
# Provenance (`isolation`) is recorded in the `provisioning` operation BEFORE any Git mutation and
# frozen into the baseline afterwards. Every later operation routes to the recorded worktree and
# never derives it from the caller's checkout. Git is required; there is no filesystem-copy mode.
DELIVERY_WORKTREE='workspace'
_WORK_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*')
DIRTY_SOURCE=('dirty_source_worktree: Cannot start isolated delivery from a dirty source worktree. '
              'Commit, stash, or remove local changes first.')


def _source_isolation(source,root):
    """Exact committed base + deterministic identities; reads the source checkout, never writes it."""
    repo=_Repo(source)
    try:identity=repo.identity()
    except RecoveryBlocked:raise RecoveryBlocked('DELIVERY_BLOCKED','source_not_a_git_branch_checkout') from None
    except ValueError as exc:raise RecoveryBlocked('DELIVERY_BLOCKED','source_repository_unsupported: '+str(exc)) from None
    # Same visibility as the recovery snapshot (.gitignore/exclude). The config file alone is exempt,
    # as in drift checks: the work runs on the config text frozen into its baseline, not the file.
    raw=repo.git('status','--porcelain=v1','-z','--no-renames','--untracked-files=all')
    dirty=sorted({os.fsdecode(e[3:]) for e in raw.split(b'\0') if e}-{CONFIG_PATH})
    if dirty:raise RecoveryBlocked('DELIVERY_BLOCKED',DIRTY_SOURCE,dirty)
    work_id=root.name
    if not _WORK_ID.fullmatch(work_id) or len(work_id)>100 or work_id.endswith('.lock'):
        raise RecoveryBlocked('DELIVERY_BLOCKED','work_id_not_a_safe_branch_name')
    if root==repo.workspace or root.is_relative_to(repo.workspace):
        raise RecoveryBlocked('DELIVERY_BLOCKED','work_root_inside_source_worktree')
    return dict(version=1,repository=repo.common_dir(),source_worktree=str(repo.workspace),source_branch=identity['branch'],
                base_sha=identity['head'],delivery_branch='refs/heads/switchyard/'+work_id,
                delivery_worktree=str(root/DELIVERY_WORKTREE))


def _branch_head(repo,branch):
    code,out=repo.probe('rev-parse','--verify','--quiet',branch+'^{commit}')
    return out.decode().strip() if code==0 else None


def _worktrees(repo):
    """{resolved worktree path: checked-out ref or None} as the repository itself records them."""
    listed,path={},None
    for line in repo.git('worktree','list','--porcelain','-z').split(b'\0'):
        if line.startswith(b'worktree '):path=str(Path(os.fsdecode(line[9:])).resolve());listed[path]=None
        elif line.startswith(b'branch ') and path:listed[path]=os.fsdecode(line[7:])
    return listed


def _check_isolation(iso,terminal,*,pristine=False):
    """The recorded worktree still exists, belongs to the recorded repository, has the delivery
    branch checked out, and descends from base_sha (exactly base_sha and clean when `pristine`)."""
    path=Path(iso['delivery_worktree'])
    if path.is_symlink() or not path.is_dir():
        raise RecoveryBlocked(terminal,'delivery_worktree_missing',[str(path)])
    repo=_Repo(path)
    code,top=repo.probe('rev-parse','--show-toplevel')
    if code or Path(os.fsdecode(top.strip())).resolve()!=path or repo.common_dir()!=iso['repository']:
        raise RecoveryBlocked(terminal,'delivery_worktree_not_owned',[str(path)])
    if _worktrees(repo).get(str(path))!=iso['delivery_branch']:
        raise RecoveryBlocked(terminal,'delivery_branch_mismatch',[iso['delivery_branch']])
    head=_branch_head(repo,'HEAD')
    if pristine:
        if head!=iso['base_sha'] or repo.git('status','--porcelain=v1','-z','--untracked-files=all'):
            raise RecoveryBlocked(terminal,'delivery_worktree_not_pristine',[str(path)])
    elif head is None or repo.probe('merge-base','--is-ancestor',iso['base_sha'],head)[0]!=0:
        raise RecoveryBlocked(terminal,'delivery_base_mismatch',[iso['base_sha']])
    return repo


def _provision_preflight(iso):
    """A fresh start owns nothing yet: any existing branch or path is someone else's."""
    path=Path(iso['delivery_worktree'])
    if _branch_head(_Repo(iso['source_worktree']),iso['delivery_branch']) is not None:
        raise RecoveryBlocked('DELIVERY_BLOCKED','delivery_branch_exists',[iso['delivery_branch']])
    if path.exists() or path.is_symlink():
        raise RecoveryBlocked('DELIVERY_BLOCKED','delivery_worktree_path_exists',[str(path)])


def _provision(iso,pristine=True):
    """Idempotent after the durable intent: create the branch at base_sha, then its worktree. A retry
    adopts an existing branch/worktree only if it exactly matches the recorded provenance (branch at
    base_sha, clean worktree of this repository on that branch). Nothing is ever reset or deleted."""
    source=_Repo(iso['source_worktree'])
    if source.common_dir()!=iso['repository']:raise RecoveryBlocked('DELIVERY_BLOCKED','source_repository_changed')
    path=Path(iso['delivery_worktree']);branch=iso['delivery_branch']
    head=_branch_head(source,branch)
    if head is None:
        zero='0'*len(iso['base_sha'])  # Create-only compare-and-swap: never moves an existing ref.
        source.git('update-ref','-m','switchyard: delivery branch for '+path.parent.name,branch,iso['base_sha'],zero)
    elif head!=iso['base_sha']:
        raise RecoveryBlocked('DELIVERY_BLOCKED','delivery_branch_conflict',[branch])
    if not (path.exists() or path.is_symlink()):
        if iso['delivery_branch'] in _worktrees(source).values():
            raise RecoveryBlocked('DELIVERY_BLOCKED','delivery_branch_checked_out_elsewhere',[branch])
        # Repository hooks do not run for Switchyard's own checkout.
        source.git('-c','core.hooksPath='+os.devnull,'worktree','add',str(path),branch[len('refs/heads/'):])
    return _check_isolation(iso,'DELIVERY_BLOCKED',pristine=pristine)


def _execution(baseline,config,terminal):
    """The work's execution workspace and the provider config routed into it. Isolated work always
    runs in its recorded delivery worktree; legacy in-place work keeps the caller's checkout."""
    iso=baseline.get('isolation')
    if iso is None:return _Repo(config.working_directory),config
    repo=_check_isolation(iso,terminal)
    if str(Path(config.working_directory).resolve()) not in (iso['source_worktree'],iso['delivery_worktree']):
        raise RecoveryBlocked(terminal,'provider_workspace_mismatch')
    return repo,replace(config,working_directory=iso['delivery_worktree'])


def _baseline(repo,store,config,flow,isolation=None,extra=None):
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
    _put(store,'baseline',dict(snapshot=snap,sources=sources,index_blob=_blob(store,index),config=config,flow=flow,lifecycle_contract_version=2,
                                **({'isolation':isolation} if isolation else {}),**(extra or {})))
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
        except ProviderExecutionError as exc:
            # Semantic boundary: the adapter returned no StageResult. Anything raised after
            # it returns (contract checks, artifact writes, transitions) stays charged.
            exc.before_stage_result=True
            raise
        finally:
            # Adapter transport waits for provider termination before returning/raising.
            op=_read(self.store,'operation')
            if op.get('provider_pid') is None:
                _checkpoint(self.store,self.repo,self.root)


def _compiled(project,flow,workspace,config,store,repo,root):
    provider=ProviderStageExecutorResolver(project.bundle.profiles,config)
    return compile_workflow(project.bundle,flow,workspace_resolver(project.bundle,flow,workspace,
        lambda binding:_CheckpointExecutor(provider(binding),store,repo,root)))


_PROVIDER_REASONS=('provider_failure','provider_timeout')
_TAIL_BYTES=1024
# First match wins. A provider that answered but exhausted StageOutputContract repair produced
# no StageResult: invalid_stage_result, still an infrastructure (non-semantic) failure.
_CLASSES=((re.compile(r'subtype=error_max_structured_output_retries|terminal_reason=structured_output_retry_exhausted'),'invalid_stage_result'),
          (re.compile(r'api_error_status=429|\brate[ _-]?limit',re.I),'rate_limit'),
          (re.compile(r'api_error_status=40[13]|authenticat|unauthori[sz]ed|not logged in|invalid api key|please run /login|\blogin\b',re.I),'authentication'),
          (re.compile(r'api_error_status=5\d\d|overloaded|service unavailable|temporarily unavailable',re.I),'provider_unavailable'))


def _classify(cause):
    message=str(cause)
    if message=='process timeout':return 'timeout'
    if message.startswith('cannot start executable'):return 'provider_unavailable'
    if message=='process I/O failure':return 'transport_failure'
    if cause.returncode is None and not message.startswith('process terminated by signal'):return 'invalid_stage_result'
    text=cause.diagnostics.utf8.decode('utf-8') if cause.diagnostics is not None else ''
    return next((kind for pattern,kind in _CLASSES if pattern.search(text)),'provider_exit')


def _provider_failure(cause,binding,started):
    """Bounded infrastructure facts; the output tail is already credential-redacted by adapters."""
    tail=cause.diagnostics.utf8[-_TAIL_BYTES:].decode('utf-8',errors='ignore') if cause.diagnostics is not None else None
    return dict(classification=_classify(cause),provider=getattr(binding,'provider',None),model=getattr(binding,'model',None),
                agent=getattr(binding,'profile_name',None),exit_code=cause.returncode,timed_out=str(cause)=='process timeout',
                duration_ms=None if started is None else max(0,(time.time_ns()-started)//1_000_000),output_tail=tail or None)


def _drive(project,flow,request,root,config,store,repo,history=None,recover_to=None,void=frozenset(),human_input=None,human_resume_to=None,start=None):
    workspace=WorkWorkspace.open(root,project.bundle.artifacts);workspace.initialize_request(request)
    compiled=None
    try:
        def observe(event,pid):
            old=_read(store,'operation')
            old.update(provider_pid=pid if event=='started' else None, process_confirmed_dead=event=='terminated')
            _put(store,'operation',old)
        config=replace(config,process_observer=observe)
        compiled=_compiled(project,flow,workspace,config,store,repo,root)
        runtime_flow=compiled.flow if start is None else replace(compiled.flow,initial=compiled.entry_states[start])
        result=run(WorkUnit(request),runtime_flow,compiled.executors,
                   recorder=HumanInputRecorder(workspace,compiled,
                       _Recorder(Path(root)/'events.jsonl',store,compiled,root),
                       before_wait=lambda:_checkpoint(store,repo,root)),resume_from=history,recover_to=recover_to,
                   void_invocations=void,human_input=human_input,human_resume_to=human_resume_to)
    except BaseException as exc:
        reason='execution_interrupted';cause=exc
        while cause is not None:
            if isinstance(cause,ProviderExecutionError):
                reason='provider_timeout' if str(cause)=='process timeout' else 'provider_failure';break
            if isinstance(cause,EvidenceDeliveryError):
                reason='qa_evidence_undeliverable';break  # Refused before launch; keep its exact diagnostic.
            cause=cause.__cause__
        old=_read(store,'operation') if store.versions('operation') else {}
        if old.get('provider_pid') is not None:
            raise  # Cleanup could not prove termination; keep RUNNING and refuse destructive recovery.
        _checkpoint(store,repo,root)
        safe=reason.startswith('provider_') and old.get('manifest_before')==_manifest(root)
        last=getattr(exc,'events',())[-1:]
        invocation=last[0].invocation_id if last and last[0].kind=='run_error' else None
        # Infrastructure failure: no StageResult, no artifact change. It is charged to the
        # agent's infrastructure_failures allowance, never to semantic invocation budgets.
        infra=safe and invocation is not None and getattr(cause,'before_stage_result',False)
        fields=dict(invocation_id=invocation,semantic_invocation=not infra)
        if infra:
            binding=compiled.plan[last[0].state].binding if compiled is not None else None
            fields['provider_failure']=_provider_failure(cause,binding,old.get('timestamp'))
        _operation(store,'interrupted',reason=reason,state=old.get('state'),rung=old.get('rung'),
                   manifest_before=old.get('manifest_before'),diagnostic=str(cause or exc)[:2048],
                   returncode=getattr(cause,'returncode',None),resume_safe=safe,**fields)
        if reason=='qa_evidence_undeliverable':
            raise EvidenceDeliveryError(str(cause)) from exc  # Callers see the explicit diagnostic.
        raise
    _checkpoint(store,repo,root)
    if result.paused:
        _operation(store,'waiting_for_human',state=result.terminal_state,request_id=result.events[-1].state_visit_id)
    else:
        waits = [e for e in result.events if e.kind=='budget_exceeded' and compiled.flow.states[e.state].wait_for=='human_input']
        details = dict(reason='human_input_round_limit', diagnostic=f'Clarification limit {waits[-1].limit} reached for {waits[-1].state}; review the request before starting further work.') if waits else {}
        _complete_delivery(root,store,repo,flow,workspace,result,details)
    return WorkflowExecution(result,workspace,compiled)


def execute_recoverable_v2_work(config_text,flow_name,request,work_root,provider_runtime_config,*,isolation=True):
    """New delivery with a baseline before any provider invocation.

    Isolated (default): `provider_runtime_config.working_directory` is the clean source checkout.
    Durable order: source preflight (no writes) -> `provisioning` operation with the provenance ->
    branch at base_sha -> worktree -> baseline of the worktree (provenance frozen) -> `running`.
    Retrying the same start after a crash anywhere before `running` converges on the same branch
    and worktree. `isolation=False` is the legacy in-place mode (dirty baselines, caller's checkout)."""
    root=Path(work_root).resolve()
    project=simple_config_from_text(config_text)
    project.bundle.flow(flow_name)
    with _lock(root):
        if (root/'manifest.jsonl').exists():raise RecoveryBlocked('RECOVERY_BLOCKED','workspace_not_fresh')
        retry=(root/'recovery/manifest.jsonl').exists()
        if retry:
            store=WorkWorkspace.open(root/'recovery',CATALOG)
            op=_read(store,'operation') if store.versions('operation') else {}
            if not isolation or op.get('phase')!='provisioning':
                raise RecoveryBlocked('RECOVERY_BLOCKED','workspace_not_fresh')
            if store.read_artifact('request')!=request:raise RecoveryBlocked('RECOVERY_BLOCKED','delivery_start_differs')
            iso=op['isolation']
        elif isolation:
            iso=_source_isolation(provider_runtime_config.working_directory,root)  # Refuses before any write.
            _provision_preflight(iso)
            store=WorkWorkspace.open(root/'recovery',CATALOG)
            store.initialize_request(request)
            _operation(store,'provisioning',isolation=iso)
        else:
            store=WorkWorkspace.open(root/'recovery',CATALOG)
            store.initialize_request(request)
        if isolation:
            repo=_provision(iso)
            if store.versions('baseline'):
                baseline=_read(store,'baseline')
                if (baseline['config'],baseline['flow'],baseline.get('isolation'))!=(config_text,flow_name,iso):
                    raise RecoveryBlocked('RECOVERY_BLOCKED','delivery_start_differs')
                _drift(baseline['snapshot'],repo.snapshot()[0],'DELIVERY_BLOCKED')
            else:_baseline(repo,store,config_text,flow_name,iso)
            provider_runtime_config=replace(provider_runtime_config,working_directory=iso['delivery_worktree'])
        else:
            repo=_Repo(provider_runtime_config.working_directory)
            _baseline(repo,store,config_text,flow_name)
        _operation(store,'running')
        return _drive(project,flow_name,request,root,provider_runtime_config,store,repo)


def _operations(store):
    return [_read_version(store,'operation',r.version) for r in store.versions('operation')]


def _read_version(store,name,version):return json.loads(store.read_artifact(name,version))


def _legacy_interruption(op,history,compiled,root):
    """Switchyard <=2.2 interruption records lack `semantic_invocation`. They set a provider_*
    reason only for ProviderExecutionError, which adapters raise strictly before returning a
    StageResult, and resume_safe only with an unchanged artifact manifest. With the retained
    unfinished run_error that is sufficient proof that no semantic result existed."""
    if op['phase']!='interrupted' or 'semantic_invocation' in op:return None
    if op.get('reason') not in _PROVIDER_REASONS or op.get('resume_safe') is not True:return None
    if not history or op.get('manifest_before')!=_manifest(root):return None
    last=history[-1]
    if last.kind!='run_error' or last.invocation_id is None or last.outcome is not None:return None
    if any(e.kind=='invocation_finished' and e.invocation_id==last.invocation_id for e in history):return None
    binding=compiled.plan[last.state].binding
    if binding is None or (binding.logical_state,binding.rung_index)!=(op.get('state'),op.get('rung')):return None
    return last.invocation_id


def _void(ops,history,compiled,root):
    """Invocation ids proven to have ended without a StageResult; append-only evidence."""
    void=set()
    for op in ops:
        if op['phase']=='interrupted' and op.get('semantic_invocation') is False and op.get('invocation_id'):
            void.add(op['invocation_id'])
        inferred=op.get('inferred_interruption')
        if op['phase']=='running' and type(inferred) is dict and inferred.get('semantic_invocation') is False:
            void.add(inferred['invocation_id'])
    legacy=_legacy_interruption(ops[-1],history,compiled,root) if ops else None
    if legacy:void.add(legacy)
    return frozenset(void)


def _accounting(project,compiled,history,void,enter=None):
    """Semantic budgets and infrastructure allowance for the visit that would execute next."""
    name=enter or history[-1].state
    state=compiled.flow.states[name]
    inv=[e for e in history if e.kind=='invocation_started' and e.invocation_id not in void]
    scopes=[]
    if compiled.flow.max_invocations_per_run is not None:
        scopes.append(dict(scope='run',used=len(inv),limit=compiled.flow.max_invocations_per_run))
    for scope in state.invocation_budgets:
        relevant = inv
        if scope.per_visit_of is not None:
            if enter == scope.per_visit_of:
                relevant = []
            else:
                last = max((i for i,e in enumerate(history) if e.kind=='state_entered' and e.state==scope.per_visit_of), default=-1)
                retained = {e.invocation_id for e in history[last+1:] if e.kind=='invocation_started'}
                relevant = [e for e in inv if e.invocation_id in retained]
        scopes.append(dict(scope=scope.name,used=sum(any(s.name==scope.name for s in compiled.flow.states[e.state].invocation_budgets) for e in relevant),limit=scope.limit))
    tight=min(scopes,key=lambda s:(s['limit']-s['used'],s['limit']),default=None)
    agent=compiled.plan[name].binding.profile_name
    failures=sum(e.kind=='invocation_started' and e.invocation_id in void and compiled.plan[e.state].binding.profile_name==agent for e in history)
    visits=None
    if enter is not None and state.max_state_visits_per_run is not None:
        visits=dict(used=sum(e.kind=='state_entered' and e.state==enter for e in history),limit=state.max_state_visits_per_run)
    return dict(semantic_invocations_used=None if tight is None else tight['used'],
                semantic_invocations_limit=None if tight is None else tight['limit'],
                semantic_budgets=scopes,infrastructure_agent=agent,infrastructure_failures_used=failures,
                infrastructure_failures_limit=project.agents[agent].infrastructure_failures,visits=visits)


def _refusal(accounting):
    """Why the interrupted visit, or a new visit of state `enter`, may not execute; None if it may."""
    if accounting['visits'] and accounting['visits']['used']>=accounting['visits']['limit']:return 'invocation_budget_exhausted'
    if any(s['used']>=s['limit'] for s in accounting['semantic_budgets']):return 'invocation_budget_exhausted'
    if accounting['infrastructure_failures_used']>=accounting['infrastructure_failures_limit']:return 'infrastructure_budget_exhausted'
    return None


class _Projection:
    def run(self,request):raise AssertionError('projection only')


def _frozen_project(baseline, config_text=None):
    """Version dispatch is selected by immutable baseline, never current project text."""
    version = baseline.get('lifecycle_contract_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise RecoveryBlocked('RECOVERY_BLOCKED', 'unsupported_lifecycle_contract_version')
    text = baseline['config'] if config_text is None else config_text
    if version == 1:
        from .legacy_config import read_legacy_project
        return read_legacy_project(text)
    return simple_config_from_text(text)


def _route(project, flow, terminal):
    """Only a frozen, explicitly recoverable workflow terminal supplies a route."""
    spec = project.bundle.flow(flow).states.get(terminal)
    return spec.resume_to if spec is not None and spec.kind == 'recoverable_terminal' else None


def _qa_route(project, flow):
    """The corrective entry state declared by the frozen QA terminal (`resume_to`), if reopen is enabled."""
    lifecycle = getattr(project, 'lifecycle', None)
    spec = project.bundle.flow(flow).states.get(QA_READY)
    if lifecycle is None or not lifecycle.reopen or spec is None or spec.kind != 'recoverable_terminal':
        return None
    return spec.resume_to


def _state_status(project, flow, state):
    spec = project.bundle.flow(flow).states.get(state)
    return dict(state_kind=None if spec is None else spec.kind,
                workflow_terminal=False if spec is None else spec.terminal,
                waiting=False if spec is None else spec.kind == 'wait',
                wait_for=None if spec is None else spec.wait_for,
                declared_resume_to=None if spec is None else spec.single_resume_to)


def resume_v2_work(work_root,config_text,provider_runtime_config,reason=''):
    """Human-requested continuation of the same run; no budget reset.

    An interrupted visit is re-entered. A recoverable workflow terminal
    continues only into the state its frozen workflow declares with `resume_to`.
    `reason` is bounded recovery metadata and never influences routing.
    """
    if type(reason) is not str or len(reason.encode('utf-8'))>4096:
        raise RecoveryBlocked('RESUME_BLOCKED','reason_too_large')
    root=Path(work_root).resolve()
    with _lock(root):
        store=_store(root);baseline=_read(store,'baseline');op=_read(store,'operation')
        if op['phase']=='suspending':raise RecoveryBlocked('RESUME_BLOCKED','suspend_in_progress: retry suspend')
        if op['phase'] in ('suspended','restoring'):
            # Restore first (control-plane work: no provider, no budget), then continue the suspended
            # operation exactly as before. Workflow terminals and human waits are restored only.
            from .suspend import restore_suspended
            op=restore_suspended(root,store,baseline,op)
            if op['phase']=='waiting_for_human' or (op['phase']=='delivery_done' and not _rejection_pending(store,op,root)):
                return recovery_status(root)  # A durable but unfinished qa-fix rejection continues below.
        target=None
        if op['phase']=='waiting_for_human':
            return _continue_human(root,store,config_text,provider_runtime_config)
        if op['phase']=='reopened':
            return _continue_corrective(root,store,baseline,config_text,provider_runtime_config,reason)
        if _rejection_pending(store,op,root):
            return _complete_qa_fix(root,store,baseline,config_text,provider_runtime_config)
        if op['phase']=='interrupted' and op.get('reason')==PUBLICATION_PENDING:
            return _finish_publication(root,store,baseline,provider_runtime_config)
        if op['phase']=='delivery_done':
            target=_route(_frozen_project(baseline),baseline['flow'],op.get('terminal'))
            if target is None or store.versions('qa_reference'):
                raise RecoveryBlocked('RESUME_BLOCKED','no_declared_recovery_route')
        elif op['phase']!='interrupted' or not op.get('resume_safe'):
            raise RecoveryBlocked('RESUME_BLOCKED','not_a_resumable_interruption')
        try:project=_frozen_project(baseline,config_text)
        except ValueError as exc:
            raise RecoveryBlocked('RESUME_BLOCKED','invalid_configuration: '+str(exc)[:512]) from None
        if _semantic(config_text)!=_semantic(baseline['config']):
            raise RecoveryBlocked('RESUME_BLOCKED','semantic_configuration_changed_new_delivery_required')
        repo,provider_runtime_config=_execution(baseline,provider_runtime_config,'RESUME_BLOCKED');snap,_=repo.snapshot()
        checkpoint=_read(store,'checkpoint')
        _drift(checkpoint['snapshot'],snap,'RESUME_BLOCKED',ignore=(CONFIG_PATH,))
        history=tuple(read_events(root/'events.jsonl'));summarize_runs(history)
        end=('run_finished',op.get('terminal')) if target else ('run_error',)
        if not history or (history[-1].kind,history[-1].state)[:len(end)]!=end:
            raise RecoveryBlocked('RESUME_BLOCKED','incomplete_event_history')
        compiled=compile_workflow(project.bundle,baseline['flow'],lambda b:_Projection())
        enter=compiled.entry_states[target] if target else None
        ops=_operations(store);void=_void(ops,history,compiled,root)
        refusal=_refusal(_accounting(project,compiled,history,void,enter))
        if refusal:raise RecoveryBlocked('RESUME_BLOCKED',refusal)
        workspace=WorkWorkspace.open(root)
        if _manifest(root)!=(checkpoint['manifest'] if target else op['manifest_before']):
            raise RecoveryBlocked('RESUME_BLOCKED','artifact_history_changed')
        if target:
            missing=[a for a in project.bundle.flow(baseline['flow']).states[target].consumes if not workspace.versions(a)]
            if missing:raise RecoveryBlocked('RESUME_BLOCKED','recovery_inputs_missing',missing)
            _operation(store,'running',recovery=dict(from_terminal=op['terminal'],resume_to=target,reason=reason))
        else:
            legacy=_legacy_interruption(op,history,compiled,root)
            # Persist the historical inference append-only; earlier records are never edited.
            _operation(store,'running',**({'recovery':dict(reason=reason)} if reason else {}),
                       **({'inferred_interruption':dict(invocation_id=legacy,semantic_invocation=False,
                           basis='switchyard<=2.2 provider interruption without StageResult')} if legacy else {}))
        return _drive(project,baseline['flow'],workspace.read_artifact('request'),root,provider_runtime_config,store,repo,history,enter,void)


def recovery_status(work_root):
    store=_store(work_root);op=_read(store,'operation')
    if op['phase']=='provisioning':
        raise RecoveryBlocked('RECOVERY_BLOCKED','adoption_incomplete: retry the same adopt' if op.get('adoption')
                              else 'delivery_provisioning_incomplete: retry the same delivery start')
    base=_read(store,'baseline')
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
                if not alive and _restore_wait(work_root,store):
                    op=_read(store,'operation');phase=op['phase']
                elif not alive:
                    op=_operation(store,'interrupted',state=op.get('state'),reason='execution_interrupted',
                                  resume_safe=False,checkpoint_uncertain=True)
                    phase='interrupted'
        except RecoveryBlocked:pass  # Another controller still owns the active invocation.
    if phase=='waiting_for_human':
        return _human_status(work_root,store)
    terminal={'running':'RUNNING','interrupted':'INTERRUPTED','reopened':'REOPENED','rolled_back':'ROLLED_BACK','rollback_running':'ROLLBACK_BLOCKED','accepted':'ACCEPTED',
              'suspending':'SUSPENDING','suspended':'SUSPENDED','restoring':'RESTORING'}.get(phase,op.get('terminal'))
    if phase=='delivery_done' and op.get('terminal')==QA_READY and store.versions('qa_reference'):
        ref=_read(store,'qa_reference')
        if not (Path(ref['workspace'])/'manifest.jsonl').is_file():
            raise RecoveryBlocked('RECOVERY_BLOCKED','qa_workspace_unavailable')
        qa=WorkWorkspace.open(ref['workspace'])
        if qa.versions('qa_decision'):terminal=_read(qa,'qa_decision')['terminal']
        if _rejection_pending(store,op,work_root):terminal='REOPENED'  # Durably requested, not yet finalized.
    project=_frozen_project(base)
    held=op['previous_operation'] if phase in SUSPEND_PHASES else op  # Suspension keeps the workflow position.
    workflow_state=held.get('terminal') if held['phase']=='delivery_done' else (QA_READY if held['phase'] in ('reopened','accepted') else held.get('state'))
    lifecycle=_state_status(project,base['flow'],workflow_state)
    recovery_source='runtime_interruption' if phase=='interrupted' else ('workflow' if phase=='delivery_done' else None)
    resumable=False;route=None;accounting={};blocked=None
    try:
        if phase=='delivery_done' and not store.versions('qa_reference'):
            route=_route(project,base['flow'],terminal)
        if phase=='reopened' or _rejection_pending(store,op,work_root):
            route=_qa_route(project,base['flow'])
        if phase=='interrupted' and op.get('reason')==PUBLICATION_PENDING:
            route,resumable=QA_READY,True
        elif route or (phase=='interrupted' and op.get('resume_safe')):
            compiled=compile_workflow(project.bundle,base['flow'],lambda b:_Projection())
            history=tuple(read_events(Path(work_root)/'events.jsonl'))
            void=_void(_operations(store),history,compiled,Path(work_root).resolve())
            enter=compiled.entry_states[route] if route else None
            accounting=_accounting(project,compiled,history,void,enter)
            if not route:
                accounting['semantic_invocation_charged']=history[-1].invocation_id not in void
            blocked=_refusal(accounting);del accounting['visits']
            resumable=blocked is None
            route=route or (op.get('state') if resumable else None)
    except Exception:pass
    if phase in ('suspended','restoring'):resumable=True  # `resume` restores the shelf first.
    qa_status = {}
    if store.versions('qa_reference'):
        ref=_read(store,'qa_reference');qa_ws=WorkWorkspace.open(ref['workspace'])
        requests,resolved=fix_records(WorkWorkspace.open(work_root))
        sequence=json.loads(qa_ws.read_artifact('qa_candidate')).get('sequence',1)
        qa_status=dict(qa_candidate=dict(sequence=sequence,identity=ref['identity'],workspace=ref['workspace'],
                            decision=_read(qa_ws,'qa_decision')['terminal'] if qa_ws.versions('qa_decision') else None),
                        qa_fix_rounds=len(requests),qa_fix_open=len(requests)>len(resolved),
                        current_candidate_sequence=sequence,current_qa_round=len(requests) or None,
                        qa_round_open=len(requests)>len(resolved))
        if phase=='accepted':
            qa_status.update({k:op[k] for k in ('accepted_commit_sha','accepted_candidate_identity','accepted_candidate_sequence')})
        elif base.get('isolation') and qa_status['qa_candidate']['decision']=='ACCEPTED':
            # Durable decision, work record not yet written: an explicitly recoverable state (retry accept).
            qa_status.update(accepted_commit_sha=_read(qa_ws,'qa_decision')['committed_head'],
                             accepted_candidate_identity=ref['identity'],acceptance_finalization_pending=True)
        counts={state:0 for state in ('open','fixed','verified')}
        for item in qa_history(work_root,include_evidence=False)['rounds']:
            for finding in item['findings']:counts[finding['status']]+=1
        qa_status.update(qa_open_findings=counts['open'],qa_fixed_findings=counts['fixed'],qa_verified_findings=counts['verified'])
    human_history = {}
    if (Path(work_root)/'human_input/manifest.jsonl').is_file():
        question, answer = active_request(WorkWorkspace.open(work_root))
        human_history = dict(human_input_pending=False, human_input_request_id=question['request_id'],
                             human_input_round=question['round'], human_input_answered=answer is not None,
                             resume_targets=list(request_targets(question)),
                             selected_resume_to=answer_target(question,answer) if answer else None,
                             question=question['question'], human_answer=answer['answer'] if answer else None,
                             origin_state=question['origin_state'],origin_outcome=question['origin_outcome'])
    return dict(work_id=Path(work_root).name,target=base['snapshot']['identity']['target'],flow=base['flow'],phase=phase,terminal=terminal,
                state=op.get('state'),workflow_state=workflow_state,reason=op.get('reason'),diagnostic=op.get('diagnostic'),resume_allowed=resumable,resume_to=route,
                recovery_source=recovery_source,**lifecycle,**_isolation_status(base,op),**_suspend_status(op),**_adoption_status(base),
                **accounting,**human_history,**qa_status,**({'resume_blocked':blocked} if blocked else {}),
                **({'provider_failure':op['provider_failure']} if phase=='interrupted' and 'provider_failure' in op else {}),
                rollback_allowed=phase in ('interrupted','delivery_done','rollback_running','reopened') and not op.get('checkpoint_uncertain',False) and terminal!='ACCEPTED')


def _isolation_status(base,op=None):
    """Debug view of the frozen isolation provenance; a failed check is reported, never repaired.
    A worktree removed by a durable suspend is healthy; a missing active worktree stays an error."""
    iso=base.get('isolation')
    if iso is None:return dict(isolated=False)
    removed=op is not None and op['phase']=='suspended' and op.get('worktree_removed') is True
    restoring=op is not None and op['phase']=='restoring'  # Partially recreated: never reported healthy.
    problem=None
    if not (removed or restoring):
        try:_check_isolation(iso,'RECOVERY_BLOCKED')
        except RecoveryBlocked as exc:problem=exc.reason
    return dict(isolated=True,base_sha=iso['base_sha'],delivery_branch=iso['delivery_branch'],
                delivery_worktree=iso['delivery_worktree'],source_worktree=iso['source_worktree'],
                delivery_worktree_state='removed' if removed else ('restoring' if restoring else ('invalid' if problem else 'present')),
                **({'isolation_blocked':problem} if problem else {}))


SUSPEND_PHASES=('suspending','suspended','restoring')


def _adoption_status(base):
    """Compact continuity view (#5): where reused product came from; never W1's history."""
    adoption=base.get('adoption')
    if adoption is None:return {}
    return dict(adopted=True,adopted_from_work_id=adoption['from_work_id'],adopted_from_base_sha=adoption['from_base_sha'],
                adopted_implementation_source=adoption['implementation_source'],adoption_entry_state=adoption['entry_state'],
                reused_request=True,reused_specification=adoption['reused_specification'],reused_implementation=True,
                reused_artifacts=sorted(adoption['reused_artifacts']),reused_sources=adoption['reused_sources'],
                transplanted='transplant' in adoption,
                **({'transplant_from_base_sha':adoption['transplant']['from_base'],
                    'transplant_to_base_sha':adoption['transplant']['to_base']} if 'transplant' in adoption else {}),
                **({'continuity':{action.lower():[d['stage'] for d in adoption['continuity']['decisions'] if d['action']==action]
                                  for action in ('REUSED','RERUN','INVALIDATED')}} if 'continuity' in adoption else {}))


def _suspend_status(op):
    if op['phase'] not in SUSPEND_PHASES:return {}
    shelf=op.get('shelf') or {}
    return dict(suspended=op['phase'] in ('suspended','restoring'),suspend_reason=op.get('reason') or None,
                shelf_id=shelf.get('id',op.get('shelf_id')),shelf_manifest_sha256=shelf.get('manifest_sha256'),
                workspace_cleanup_pending=op['phase']=='suspended' and op.get('worktree_removed') is not True,
                restore_pending=op['phase']=='restoring',
                **({'resume_blocked':'suspend_in_progress: retry suspend'} if op['phase']=='suspending' else {}))


def _not_suspended(op,terminal):
    """SUSPENDED is intentional, not a missing worktree: only `resume` restores it."""
    if op['phase'] in ('suspended','restoring'):raise RecoveryBlocked(terminal,'work_suspended: resume restores it first')
    if op['phase']=='suspending':raise RecoveryBlocked(terminal,'suspend_in_progress: retry suspend')


def attach_qa_candidate(work_root,qa_root):
    """Bind the external QA candidate to its source recovery history."""
    with _lock(work_root):
        store=_store(work_root)
        from .qa_lifecycle import _load
        _,candidate,identity=_load(qa_root)
        if candidate['source_workspace']!=str(Path(work_root).resolve()):raise RecoveryBlocked('RECOVERY_BLOCKED','qa_source_mismatch')
        iso=_read(store,'baseline').get('isolation')
        if iso is not None and candidate['target']!=iso['delivery_worktree']:
            # A candidate captured from the source checkout would later be accepted THERE.
            raise RecoveryBlocked('RECOVERY_BLOCKED','qa_candidate_not_from_delivery_worktree')
        _put(store,'qa_reference',dict(workspace=str(Path(qa_root).resolve()),identity=identity))


def rollback_v2_work(work_root,reason=''):
    """Restore only checkpoint-owned changes; retain history and finalize QA first."""
    if type(reason) is not str or len(reason.encode('utf-8'))>4096:
        raise RecoveryBlocked('ROLLBACK_BLOCKED','reason_too_large')
    root=Path(work_root).resolve()
    with _lock(root):
        store=_store(root);base=_read(store,'baseline');op=_read(store,'operation')
        _not_suspended(op,'ROLLBACK_BLOCKED')
        if op['phase'] not in ('interrupted','delivery_done','rollback_running','waiting_for_human','reopened','accepted'):
            raise RecoveryBlocked('ROLLBACK_BLOCKED','delivery_not_in_rollback_state')
        if store.versions('qa_reference'):
            ref=_read(store,'qa_reference');qa=WorkWorkspace.open(ref['workspace'])
            if qa.versions('qa_decision') and _read(qa,'qa_decision')['terminal']=='ACCEPTED':
                raise RecoveryBlocked('ROLLBACK_BLOCKED','accepted_history_use_git_revert_or_corrective_delivery')
        if op.get('checkpoint_uncertain'):
            raise RecoveryBlocked('ROLLBACK_BLOCKED','uncertain_checkpoint_after_controller_crash')
        if 'isolation' in base:_check_isolation(base['isolation'],'ROLLBACK_BLOCKED')
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


def _restore_wait(root, store):
    """Recover only the fully committed wait boundary, never an ambiguous active invocation."""
    path = Path(root)/'events.jsonl'
    if not path.is_file():
        return False
    history = tuple(read_events(path))
    if not history or history[-1].kind != 'human_input_requested':
        return False
    summarize_runs(history)
    question, _ = active_request(WorkWorkspace.open(root))
    if question['request_id'] != history[-1].state_visit_id or _manifest(root) != _read(store,'checkpoint')['manifest']:
        return False
    _operation(store,'waiting_for_human',state=history[-1].state,request_id=question['request_id'])
    return True


def _human_status(root, store):
    base = _read(store,'baseline')
    question, answer = active_request(WorkWorkspace.open(root))
    project = _frozen_project(base)
    declaration = project.bundle.flow(base['flow']).states.get(question['wait_state'])
    if declaration is None or declaration.kind != 'wait' or declaration.resume_targets != request_targets(question):
        raise RecoveryBlocked('RECOVERY_BLOCKED','human_wait_contract_mismatch')
    compiled = compile_workflow(project.bundle,base['flow'],lambda b:_Projection())
    history = tuple(read_events(Path(root)/'events.jsonl'))
    void = _void(_operations(store),history,compiled,Path(root).resolve())
    selected = answer_target(question, answer) if answer is not None else declaration.single_resume_to
    choices = [selected] if selected is not None else declaration.resume_targets
    options = {t: _accounting(project,compiled,history,void,compiled.entry_states[t]) for t in choices}
    blocked_targets = {t: _refusal(a) for t, a in options.items()}
    available = next((t for t in choices if blocked_targets[t] is None), choices[0])
    accounting = options[available]
    blocked = blocked_targets[available]
    del accounting['visits']
    return dict(work_id=Path(root).name,target=base['snapshot']['identity']['target'],flow=base['flow'],
        phase='waiting_for_human' if answer is None else 'human_answer_ready', terminal=None,
        state=question['wait_state'],workflow_state=question['wait_state'],human_input_pending=answer is None,
        **_state_status(project,base['flow'],question['wait_state']),recovery_source='human_input',**_isolation_status(base),
        human_input_request_id=question['request_id'],human_input_round=question['round'],
        question=question['question'],blockers=question['blockers'],resume_to=declaration.single_resume_to,
        resume_targets=list(declaration.resume_targets),selected_resume_to=answer_target(question,answer) if answer else None,
        origin_state=question['origin_state'],origin_outcome=question['origin_outcome'],
        target_blockers=blocked_targets, accounting_target=available,
        human_answer=answer['answer'] if answer else None,
        answer_allowed=answer is None and blocked is None,
        resume_allowed=blocked is None,
        resume_blocked=blocked,
        next_action='answer required' if answer is None else 'resume',rollback_allowed=True,**accounting)


def _continue_human(root,store,config_text,config,request_id=None,answer_text=None,resume_to=None):
    workspace = WorkWorkspace.open(root)
    question, answer = active_request(workspace)
    if request_id is None and answer is None:
        error = RecoveryBlocked('RESUME_BLOCKED','human_input_required: use answer for '+question['request_id'])
        error.human_input_request = question
        raise error
    if request_id is not None and (request_id != question['request_id'] or answer is not None):
        raise RecoveryBlocked('ANSWER_BLOCKED','stale_or_already_answered_request')
    base = _read(store,'baseline')
    project = _frozen_project(base,config_text)
    if _semantic(config_text) != _semantic(base['config']):
        raise RecoveryBlocked('RESUME_BLOCKED','semantic_configuration_changed_new_delivery_required')
    frozen = _frozen_project(base)
    # Retune execution settings only; runtime routing also uses the frozen flows.
    project = replace(project, bundle=replace(project.bundle, flows=frozen.bundle.flows))
    history = tuple(read_events(root/'events.jsonl'));summarize_runs(history)
    if not history or history[-1].kind != 'human_input_requested' or history[-1].state_visit_id != question['request_id']:
        raise RecoveryBlocked('RESUME_BLOCKED','incomplete_human_wait_history')
    declaration = frozen.bundle.flow(base['flow']).states[question['wait_state']]
    if declaration.wait_for != 'human_input' or declaration.resume_targets != request_targets(question):
        raise RecoveryBlocked('RESUME_BLOCKED','human_wait_contract_mismatch')
    repo, config = _execution(base, config, 'RESUME_BLOCKED')
    checkpoint = _read(store,'checkpoint')
    _drift(checkpoint['snapshot'],repo.snapshot()[0],'RESUME_BLOCKED',ignore=(CONFIG_PATH,))
    if _manifest(root) != checkpoint['manifest']:
        raise RecoveryBlocked('RESUME_BLOCKED','artifact_history_changed')
    compiled = compile_workflow(frozen.bundle,base['flow'],lambda b:_Projection())
    void = _void(_operations(store),history,compiled,root)
    selected = answer_target(question, answer) if answer is not None else select_target(question, resume_to)
    target = compiled.entry_states[selected]
    pending = history[-1]
    if (pending.state != question['wait_state'] or pending.run_id != question['run_id']
            or pending.target != compiled.flow.states[pending.state].single_resume_to):
        raise RecoveryBlocked('RESUME_BLOCKED','human_wait_contract_mismatch')
    refusal = _refusal(_accounting(frozen,compiled,history,void,target))
    if refusal:
        raise RecoveryBlocked('RESUME_BLOCKED',refusal)
    missing = [a for a in frozen.bundle.flow(base['flow']).states[selected].consumes if not workspace.versions(a)]
    if missing:
        raise RecoveryBlocked('RESUME_BLOCKED','recovery_inputs_missing',missing)
    if request_id is not None:
        write_answer(workspace,request_id,answer_text,selected)
    # Answer publication is complete before run, provider resolution or invocation.
    _operation(store,'running',human_input_request_id=question['request_id'])
    return _drive(project,base['flow'],workspace.read_artifact('request'),root,config,store,repo,
                  history=history,void=void,human_input=question['request_id'],human_resume_to=target)


def answer_v2_work(work_root, request_id, answer, config_text, provider_runtime_config, resume_to=None):
    """Append one human answer and continue this work through its declared wait route."""
    validate_answer(answer)
    root = Path(work_root).resolve()
    with _lock(root):
        store = _store(root)
        _not_suspended(_read(store,'operation'),'ANSWER_BLOCKED')
        if _read(store,'operation')['phase'] != 'waiting_for_human':
            raise RecoveryBlocked('ANSWER_BLOCKED','work_not_waiting_for_human')
        return _continue_human(root,store,config_text,provider_runtime_config,request_id,answer,resume_to)


def _corrective_plan(root,store,baseline,config_text,config):
    """Every precondition of a corrective continuation; no durable side effects."""
    frozen=_frozen_project(baseline)
    target=_qa_route(frozen,baseline['flow'])
    if target is None:raise RecoveryBlocked('QA_FIX_BLOCKED','no_declared_reopen_route')
    try:project=_frozen_project(baseline,config_text)
    except ValueError as exc:
        raise RecoveryBlocked('RESUME_BLOCKED','invalid_configuration: '+str(exc)[:512]) from None
    if _semantic(config_text)!=_semantic(baseline['config']):
        raise RecoveryBlocked('RESUME_BLOCKED','semantic_configuration_changed_new_delivery_required')
    repo,config=_execution(baseline,config,'RESUME_BLOCKED');snap,_=repo.snapshot()
    checkpoint=_read(store,'checkpoint')
    _drift(checkpoint['snapshot'],snap,'RESUME_BLOCKED',ignore=(CONFIG_PATH,))
    history=tuple(read_events(root/'events.jsonl'));summarize_runs(history)
    if not history or (history[-1].kind,history[-1].state)!=('run_finished',QA_READY):
        raise RecoveryBlocked('RESUME_BLOCKED','incomplete_event_history')
    compiled=compile_workflow(project.bundle,baseline['flow'],lambda b:_Projection())
    enter=compiled.entry_states[target];void=_void(_operations(store),history,compiled,root)
    refusal=_refusal(_accounting(project,compiled,history,void,enter))
    if refusal:raise RecoveryBlocked('RESUME_BLOCKED',refusal)
    workspace=WorkWorkspace.open(root)
    if _manifest(root)!=checkpoint['manifest']:raise RecoveryBlocked('RESUME_BLOCKED','artifact_history_changed')
    missing=[a for a in project.bundle.flow(baseline['flow']).states[target].consumes if not workspace.versions(a)]
    if missing:raise RecoveryBlocked('RESUME_BLOCKED','recovery_inputs_missing',missing)
    return project,repo,config,history,void,enter,target


def _continue_corrective(root,store,baseline,config_text,config,reason=''):
    """REOPENED -> declared corrective entry, same run, same budgets, same history."""
    project,repo,config,history,void,enter,target=_corrective_plan(root,store,baseline,config_text,config)
    requests,resolved=fix_records(WorkWorkspace.open(root))
    _operation(store,'running',recovery=dict(from_terminal=QA_READY,resume_to=target,reason=reason,qa_fix_round=len(requests)))
    workspace=WorkWorkspace.open(root)
    return _drive(project,baseline['flow'],workspace.read_artifact('request'),root,config,store,repo,history,enter,void)


def _open_round(workspace):
    requests,resolved=fix_records(workspace)
    return requests[-1] if len(requests)>len(resolved) else None


def _rejection_pending(store,op,root):
    """qa-fix intent is durable but the work has not yet entered REOPENED (interrupted qa-fix)."""
    return (op['phase']=='delivery_done' and op.get('terminal')==QA_READY
            and _open_round(WorkWorkspace.open(root)) is not None)


def qa_fix_v2_work(work_root,feedback,config_text,provider_runtime_config,*,findings=None,attachments=None):
    """Human QA rejection of the current candidate; continue the SAME work through its declared route.

    `feedback` is one finding; `findings` (with feedback=None) is several, in order, each text or
    {'description', 'attachments'}. `attachments` belong to the single finding (refused otherwise).
    Durable order: validate + snapshot attachments, preflight (no writes) -> publish evidence (immutable,
    atomic) -> qa_fix_request (intent: feedback + findings + evidence refs) -> candidate REOPENED ->
    operation `reopened` -> corrective run. An interrupted call is finished by retrying with the same
    input or by `resume`; it never opens a second round or rejects a candidate twice.
    Only structural attachment validity is checked here: whether a provider/model can CONSUME the
    evidence is an execution concern, refused at the stage boundary after the rejection is durable."""
    inputs=finding_inputs(feedback,findings,attachments)
    descriptions=tuple(text for text,_ in inputs)
    fingerprint=tuple((text,tuple(a['sha256'] for a in files)) for text,files in inputs)
    root=Path(work_root).resolve()
    with _lock(root):
        store=_store(root);baseline=_read(store,'baseline');op=_read(store,'operation')
        pending=_open_round(WorkWorkspace.open(root)) if op['phase'] in ('reopened','delivery_done') else None
        if pending is not None and (op['phase']=='reopened' or _rejection_pending(store,op,root)):
            if tuple((f['description'],tuple(e['sha256'] for e in f.get('evidence',()))) for f in request_findings(pending))!=fingerprint:
                raise RecoveryBlocked('QA_FIX_BLOCKED','qa_fix_in_progress_with_different_feedback: use resume')
            if op['phase']=='reopened':
                return _continue_corrective(root,store,baseline,config_text,provider_runtime_config,'qa_fix')
            return _complete_qa_fix(root,store,baseline,config_text,provider_runtime_config)
        _not_suspended(op,'QA_FIX_BLOCKED')
        if op['phase']=='accepted':raise RecoveryBlocked('QA_FIX_BLOCKED','candidate_already_finalized')
        if op['phase']!='delivery_done' or op.get('terminal')!=QA_READY or not store.versions('qa_reference'):
            raise RecoveryBlocked('QA_FIX_BLOCKED','work_not_ready_for_manual_qa')
        ref=_read(store,'qa_reference')
        try:qa,candidate,identity=_load_candidate(ref['workspace'])
        except (LifecycleError,OSError):raise RecoveryBlocked('QA_FIX_BLOCKED','qa_workspace_unavailable') from None
        if identity!=ref['identity']:raise RecoveryBlocked('QA_FIX_BLOCKED','qa_candidate_identity_mismatch')
        if qa.versions('qa_decision') or qa.versions('qa_intent'):
            raise RecoveryBlocked('QA_FIX_BLOCKED','candidate_already_finalized')
        _, resolved = fix_records(WorkWorkspace.open(root))
        if resolved and resolved[-1]['superseding_identity']!=identity:
            raise RecoveryBlocked('QA_FIX_BLOCKED','qa_lineage_mismatch')
        _corrective_plan(root,store,baseline,config_text,provider_runtime_config)  # Refuse before any write.
        requests,_=fix_records(WorkWorkspace.open(root))
        # Evidence is durable BEFORE the request that references it; a crash here leaves only
        # unreferenced immutable objects that the same retry reuses (IDs are deterministic).
        evidence=evidence_plan(root,len(requests)+1,identity,inputs)
        write_fix_request(WorkWorkspace.open(root),flow=baseline['flow'],reopen_to=_qa_route(_frozen_project(baseline),baseline['flow']),
            descriptions=descriptions,candidate_workspace=ref['workspace'],identity=identity,candidate=candidate,work_id=root.name,
            evidence=evidence)
        return _complete_qa_fix(root,store,baseline,config_text,provider_runtime_config)


def _complete_qa_fix(root,store,baseline,config_text,config):
    """Finish a durably requested rejection from its record; idempotent up to the corrective run."""
    pending=_open_round(WorkWorkspace.open(root));ref=_read(store,'qa_reference')
    if pending['rejected_candidate']['identity']!=ref['identity']:
        raise RecoveryBlocked('RECOVERY_BLOCKED','qa_fix_candidate_mismatch')
    _corrective_plan(root,store,baseline,config_text,config)
    try:finish_reopen_v2_work(baseline['snapshot']['identity']['target'],ref['workspace'],pending['feedback'])
    except (LifecycleError,OSError) as exc:
        raise RecoveryBlocked('QA_FIX_BLOCKED','candidate_reopen_failed: '+str(exc)[:512]) from None
    _operation(store,'reopened',terminal='REOPENED',qa_fix_round=pending['round'],rejected_candidate=ref['identity'])
    return _continue_corrective(root,store,baseline,config_text,config,'qa_fix')


def _complete_delivery(root,store,repo,flow,workspace,result,details):
    """Corrective QA-ready is published only after its new candidate is current."""
    if result.terminal_state==QA_READY and _open_round(workspace) is not None:
        _operation(store,'interrupted',reason=PUBLICATION_PENDING,state=QA_READY,resume_safe=False)
        try:_publish_corrective_candidate(root,store,repo,flow,workspace,result)
        except Exception as exc:
            _operation(store,'interrupted',reason=PUBLICATION_PENDING,state=QA_READY,resume_safe=False,
                       diagnostic=(type(exc).__name__+': '+str(exc))[:2048])
            raise
    _operation(store,'delivery_done',terminal=result.terminal_state,**details)


def _publish_corrective_candidate(root,store,repo,flow,workspace,result):
    """Idempotent: build -> atomically name -> make current (qa_reference) -> close the round."""
    pending=_open_round(workspace);rejected=pending['rejected_candidate']
    sequence,supersedes=rejected['sequence']+1,rejected['identity']
    final=(Path(root)/'qa'/f'candidate-{sequence}').resolve()
    ref=_read(store,'qa_reference')
    if ref['identity']==supersedes:
        if final.exists():
            # A crash after the atomic rename: reuse only if it is this round's candidate and still exact.
            candidate,identity=revalidate_candidate(repo.workspace,final)
            if (candidate.get('sequence'),candidate.get('supersedes'),candidate['source_workspace'])!=(sequence,supersedes,str(Path(root).resolve())):
                raise RecoveryBlocked('RECOVERY_BLOCKED','qa_candidate_conflict')
        else:
            final.parent.mkdir(exist_ok=True)
            building=final.parent/f'.candidate-{sequence}.{uuid.uuid4().hex}'
            identity=create_qa_candidate(repo.workspace,workspace,result,flow,building,sequence=sequence,supersedes=supersedes).identity
            os.rename(building,final)
            fd=os.open(final.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        _put(store,'qa_reference',dict(workspace=str(final),identity=identity))
    elif ref['workspace']!=str(final):
        raise RecoveryBlocked('RECOVERY_BLOCKED','qa_reference_conflict')
    else:identity=ref['identity']
    resolve_fix_request(workspace,identity,sequence,final)


def _finish_publication(root,store,baseline,config):
    """Resume an interrupted/failed candidate publication; no stage is executed."""
    repo,_=_execution(baseline,config,'RESUME_BLOCKED')
    checkpoint=_read(store,'checkpoint')
    _drift(checkpoint['snapshot'],repo.snapshot()[0],'RESUME_BLOCKED',ignore=(CONFIG_PATH,))
    history=tuple(read_events(root/'events.jsonl'));summarize_runs(history)
    if not history or (history[-1].kind,history[-1].state)!=('run_finished',QA_READY):
        raise RecoveryBlocked('RESUME_BLOCKED','incomplete_event_history')
    if _manifest(root)!=checkpoint['manifest']:raise RecoveryBlocked('RESUME_BLOCKED','artifact_history_changed')
    project=_frozen_project(baseline);workspace=WorkWorkspace.open(root,project.bundle.artifacts)
    result=RunResult(QA_READY,history)
    _complete_delivery(root,store,repo,baseline['flow'],workspace,result,{})
    return WorkflowExecution(result,workspace,compile_workflow(project.bundle,baseline['flow'],lambda b:_Projection()))


def qa_history(work_root,*,include_evidence=True):
    """Read-only lineage: every candidate (oldest first) and every QA round, with their links.
    Each finding lists its evidence as verified metadata (never bytes)."""
    root=Path(work_root).resolve();store=_store(root)
    candidates=[]
    for record in store.versions('qa_reference'):
        ref=_read_version(store,'qa_reference',record.version)
        qa=WorkWorkspace.open(ref['workspace']);data=json.loads(qa.read_artifact('qa_candidate'))
        candidates.append(dict(sequence=data.get('sequence',1),identity=ref['identity'],supersedes=data.get('supersedes'),
                               workspace=ref['workspace'],decision=_read(qa,'qa_decision')['terminal'] if qa.versions('qa_decision') else None))
    by_identity={c['identity']:c for c in candidates}
    # Verification point: the ACCEPTED candidate's own qa_decision; its supersedes chain is the lineage.
    accepted=set()
    current=next((c for c in reversed(candidates) if c['decision']=='ACCEPTED'),None)
    while current is not None and current['identity'] not in accepted:
        accepted.add(current['identity']);current=by_identity.get(current['supersedes'])
    rounds=[]
    for item in qa_rounds(WorkWorkspace.open(root)):
        produced=item['produced']
        if produced is not None and produced['workspace'] is None and produced['identity'] in by_identity:
            produced=dict(produced,workspace=by_identity[produced['identity']]['workspace'])  # schema-1 record
        rounds.append(dict(item,produced=produced))
    rounds=list(finding_states(rounds,accepted))
    if include_evidence:
        rounds=[dict(r,findings=tuple(dict(f,evidence=[load_evidence(root,e['id'],e['sha256']) for e in f.get('evidence',())])
                                      for f in r['findings'])) for r in rounds]
    return dict(candidates=candidates,rounds=rounds)


_DRIFT=('base_head_changed','worktree_identity_changed','unexpected_staged_paths','unexpected_worktree_paths','candidate_drift','empty_candidate')


def _acceptance_message(work_id,identity,candidate):
    """Stable, prompt-free commit message: the candidate's own subject plus work/candidate linkage."""
    return (f"{candidate['commit_message']}\n\nSwitchyard-Work: {work_id}\n"
            f"Switchyard-Candidate: {identity}\nSwitchyard-Candidate-Sequence: {candidate.get('sequence',1)}\n")


def _accepted(store,iso,identity,sequence,commit):
    return _operation(store,'accepted',terminal='ACCEPTED',accepted_candidate_identity=identity,
                      accepted_candidate_sequence=sequence,accepted_commit_sha=commit,
                      delivery_branch=iso['delivery_branch'],base_sha=iso['base_sha'])


def accept_v2_candidate(work_root,*,git_executable=None):
    """Human ACCEPT of the work's CURRENT candidate; rejected or superseded candidates are unreachable.

    Isolated work: exactly one commit C on the delivery branch (parent = the candidate's base_head),
    recorded as the delivery result. Durable order: validate (isolation, lineage, candidate == worktree;
    no writes) -> qa_intent started -> commit object -> qa_intent prepared(C) -> branch CAS base->C ->
    index -> qa_decision ACCEPTED(C) -> operation `accepted`(C). A retry after a crash converges on the
    same C; it never commits twice and never resets the branch."""
    root=Path(work_root).resolve()
    with _lock(root):
        store=_store(root);baseline=_read(store,'baseline');op=_read(store,'operation')
        _not_suspended(op,'ACCEPT_BLOCKED')
        if op['phase']=='accepted':raise RecoveryBlocked('ACCEPT_BLOCKED','already_accepted: '+op['accepted_commit_sha'])
        if op['phase']!='delivery_done' or op.get('terminal')!=QA_READY or not store.versions('qa_reference'):
            raise RecoveryBlocked('ACCEPT_BLOCKED','work_not_ready_for_manual_qa')
        requests,resolved=fix_records(WorkWorkspace.open(root))
        if len(requests)!=len(resolved):raise RecoveryBlocked('ACCEPT_BLOCKED','corrective_cycle_open')
        ref=_read(store,'qa_reference')
        qa,candidate,identity=_load_candidate(ref['workspace'])
        if identity!=ref['identity']:raise RecoveryBlocked('ACCEPT_BLOCKED','qa_candidate_identity_mismatch')
        iso=baseline.get('isolation')
        if iso is None:  # Legacy in-place work: unchanged semantics.
            return accept_v2_work(baseline['snapshot']['identity']['target'],ref['workspace'],git_executable=git_executable)
        repo=_check_isolation(iso,'ACCEPT_BLOCKED')
        if (candidate['target'],candidate['branch'])!=(iso['delivery_worktree'],iso['delivery_branch']):
            raise RecoveryBlocked('ACCEPT_BLOCKED','candidate_not_on_delivery_branch')
        if repo.probe('merge-base','--is-ancestor',iso['base_sha'],candidate['base_head'])[0]!=0:
            raise RecoveryBlocked('ACCEPT_BLOCKED','delivery_base_mismatch')
        sequence=candidate.get('sequence',1)
        message=_acceptance_message(root.name,identity,candidate)
        if qa.versions('qa_decision'):
            decision=_read(qa,'qa_decision')
            if decision['terminal']=='ACCEPTED' and decision.get('committed_head'):
                # Crash after the decision, before the work record: finish without touching Git.
                _accepted(store,iso,identity,sequence,decision['committed_head'])
                return QaDecision(identity,'accept','ACCEPTED',None,decision['committed_head'])
            raise RecoveryBlocked('ACCEPT_BLOCKED','candidate_already_finalized')
        pending=bool(qa.versions('qa_intent'))
        if not pending or _branch_head(repo,iso['delivery_branch'])==candidate['base_head']:
            # Nothing published yet: refuse unexplained drift before any (further) durable step.
            try:revalidate_candidate(iso['delivery_worktree'],ref['workspace'],git_executable=git_executable)
            except LifecycleError as exc:
                if str(exc) in _DRIFT:raise RecoveryBlocked('ACCEPT_BLOCKED','candidate_worktree_drift: '+str(exc)) from None
                raise RecoveryBlocked('ACCEPT_BLOCKED','git_inspection_failed: '+str(exc)) from None
        # The work lock excludes every Switchyard controller of this work, and the delivery worktree and
        # candidate are Switchyard-owned, so lock files a dead controller left behind are stale.
        (Path(ref['workspace'])/'.lifecycle.lock').unlink(missing_ok=True)
        try:
            if pending:  # Interrupted accept: its index lock (taken only after the intent) is stale too.
                Path(str(repo.index_path())+'.lock').unlink(missing_ok=True)
                decision=finish_accept_v2_work(iso['delivery_worktree'],ref['workspace'],git_executable=git_executable,commit_message=message)
            else:
                decision=accept_v2_work(iso['delivery_worktree'],ref['workspace'],git_executable=git_executable,commit_message=message)
        except LifecycleError as exc:
            raise RecoveryBlocked('ACCEPT_BLOCKED',str(exc)[:512]) from None
        if decision.terminal=='ACCEPTED':_accepted(store,iso,identity,sequence,decision.committed_head)
        return decision
