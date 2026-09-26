"""Software-delivery consumers of the generic Python Flow/Executor API."""
from dataclasses import dataclass
import json
from pathlib import Path
from uuid import uuid4

from flow_config import LoadedFlow
from runtime import (ExecutorResult, Flow, InvocationRequest, InvocationResponse,
                     State, WorkUnit)
from runtime.codex import CodexConfig, CodexExecutor
from runtime.command import Command, CommandExecutor
from .acceptance import AcceptanceApproval, GitAcceptance, GitStep


_ANALYZE = '''Inspect the requested change and current repository before implementation.
Write a small analysis artifact with observed/current behavior, relevant constraints
and invariants, proposed change, affected areas, risks/architecture conflicts, and
verification approach. Do not modify production code in this investigation stage.
'''
_DIAGNOSE = '''Perform FRESH_DIAGNOSIS of the reported bug. Inspect current repository
and runtime behavior again. Previous conclusions and "do not change" assumptions
are advisory, not authoritative. Establish reproduction evidence, actual execution,
data/control flow, root cause, incorrect previous assumptions, proposed repair,
and verification approach. Write these findings to the new analysis artifact.
Do not modify production code during diagnosis if reasonably avoidable.
'''
_IMPLEMENT = '''Implement the requested change. Read the analysis artifact and current
repository. The analysis is advisory, not unquestionable truth. If it materially
contradicts repository reality and you cannot safely proceed, report inability via
the blocked artifact instead of patching symptoms. Do not commit or push.
'''
_REPAIR = '''Make ONE corrective attempt using the supplied bounded verifier diagnostics,
the analysis artifact and current code. Do not run a hidden verify/fix retry loop.
If the diagnosis is materially wrong or you cannot safely proceed, report inability
via the blocked artifact. Do not commit or push. Technical completion is not proof
of correctness; the runtime schedules deterministic verification separately.
'''


@dataclass(frozen=True)
class AgentStage:
    delegate: CodexExecutor
    instructions: str
    analysis_path: Path
    creates_analysis: bool = False

    def run(self, request):
        blocked = self.analysis_path.parent / 'blocked.txt'
        if blocked.exists():
            return InvocationResponse(ExecutorResult('failed'))
        prompt = (self.instructions + '\nRead repository AGENTS.md and applicable repository instructions.'
                  + '\nAnalysis artifact: ' + str(self.analysis_path)
                  + '\nIf unable to proceed safely, write a brief reason to: ' + str(blocked)
                  + '\nDo not delete a refusal artifact or claim verification/manual QA success.'
                  + '\nOriginal requested work:\n' + request.work_unit.task)
        response = self.delegate.run(InvocationRequest(WorkUnit(prompt), request.diagnostics))
        if response.result.outcome != 'success':
            return response
        if blocked.exists():
            return InvocationResponse(ExecutorResult('failed'))
        if self.creates_analysis:
            try:
                if not self.analysis_path.is_file() or not self.analysis_path.read_text(encoding='utf-8').strip():
                    return InvocationResponse(ExecutorResult('failed'))
            except (OSError, UnicodeError):
                return InvocationResponse(ExecutorResult('error'))
        return response


def _on(success, blocked, **extra):
    return {'success': success, 'failed': blocked, 'error': blocked,
            'timeout': blocked, 'cancelled': blocked, **extra}


def build_change(kind: str, *, workspace: str, codex_executable: str,
                 verifier_executable: str, verifier_argv: tuple[str, ...],
                 agent_timeout: float = 300, verifier_timeout: float = 120) -> LoadedFlow:
    """Instantiate one deliver/repair session; do not reuse for another run."""
    if kind not in ('deliver', 'repair'):
        raise ValueError('expected deliver or repair')
    agent = CodexExecutor(CodexConfig(codex_executable, workspace, agent_timeout))
    verifier = CommandExecutor(Command(verifier_executable, verifier_argv, workspace, verifier_timeout, 4096))
    session = Path(workspace) / '.switchyard' / 'delivery' / uuid4().hex
    session.mkdir(parents=True, exist_ok=False)
    analysis = session / 'analysis.md'
    first, implement, failure = (('ANALYZE', 'IMPLEMENT', 'NEEDS_REDIAGNOSIS') if kind == 'deliver'
                                  else ('FRESH_DIAGNOSIS', 'FIX', 'NEEDS_HUMAN'))
    states = {
        first: State('analysis', _on(implement, failure)),
        implement: State('implementation', _on('VERIFY', failure)),
        'VERIFY': State('verifier', _on('READY_FOR_MANUAL_QA', failure, failed='REPAIR_ONCE')),
        'REPAIR_ONCE': State('repair', _on('VERIFY_AGAIN', failure, budget_exceeded=failure),
                             max_state_visits_per_run=1, diagnostics_from_previous_max_bytes=4096),
        'VERIFY_AGAIN': State('verifier', _on('READY_FOR_MANUAL_QA', failure)),
        'READY_FOR_MANUAL_QA': State(terminal=True),
        failure: State(terminal=True),
    }
    verification = '\nAcceptance verification command (literal argv): ' + json.dumps([verifier_executable, *verifier_argv]) + '\n'
    executors = {
        'analysis': AgentStage(agent, (_ANALYZE if kind == 'deliver' else _DIAGNOSE) + verification, analysis, True),
        'implementation': AgentStage(agent, _IMPLEMENT + verification, analysis),
        'repair': AgentStage(agent, _REPAIR + verification, analysis),
        'verifier': verifier,
    }
    return LoadedFlow(Flow(first, states), executors)


def build_accept(*, workspace: str, git_executable: str, approval: AcceptanceApproval) -> LoadedFlow:
    """Instantiate deterministic acceptance for one explicitly reviewed patch."""
    if type(approval) is not AcceptanceApproval:
        raise TypeError('expected AcceptanceApproval')
    acceptance = GitAcceptance(workspace, git_executable, approval)
    states = {
        'CHECK_WORKTREE': State('worktree', _on('CHECK_SCOPE', 'COMMIT_BLOCKED')),
        'CHECK_SCOPE': State('scope', _on('COMMIT', 'COMMIT_BLOCKED')),
        'COMMIT': State('commit', _on('ACCEPTED', 'COMMIT_BLOCKED')),
        'ACCEPTED': State(terminal=True), 'COMMIT_BLOCKED': State(terminal=True),
    }
    return LoadedFlow(Flow('CHECK_WORKTREE', states), {
        'worktree': GitStep(acceptance, 'check_worktree'),
        'scope': GitStep(acceptance, 'check_scope'),
        'commit': GitStep(acceptance, 'commit'),
    })
