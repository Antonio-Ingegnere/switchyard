"""Opt-in structured durable context transport; control flow stays in runtime."""
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Callable, Protocol

from runtime import (DiagnosticInput, DiagnosticPayload, ExecutorResult,
                     InvocationRequest, InvocationResponse)
from .workflow_compiler import ExecutionBinding
from .work_contract import (ArtifactProducer, ArtifactRecord, HandoffRecord,
                            WorkContractError, WorkWorkspace)
from .workflow_bundle import WorkflowBundle, WorkflowStateSpec


def _require(condition, message):
    if not condition:
        raise WorkContractError(message)


def _tuples(instance, *names):
    for name in names:
        value = getattr(instance, name)
        _require(type(value) in (tuple, list), f'{name} must be a sequence')
        object.__setattr__(instance, name, tuple(value))


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    record: ArtifactRecord
    content: str


@dataclass(frozen=True, slots=True)
class StageInvocation:
    binding: ExecutionBinding
    artifacts: tuple[ArtifactSnapshot, ...]
    previous_handoff: HandoffRecord | None = None
    diagnostics: DiagnosticInput | None = None

    def __post_init__(self):
        _tuples(self, 'artifacts')


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    name: str
    content: str


@dataclass(frozen=True, slots=True)
class StageHandoff:
    summary: str
    findings: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    changed_assumptions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    def __post_init__(self):
        _tuples(self, 'findings', 'evidence', 'changed_assumptions', 'blockers')


@dataclass(frozen=True, slots=True)
class StageResult:
    outcome: str
    produced_artifacts: tuple[ProducedArtifact, ...] = ()
    handoff: StageHandoff | None = None
    diagnostics: DiagnosticPayload | None = None

    def __post_init__(self):
        _tuples(self, 'produced_artifacts')


class StageExecutor(Protocol):
    def run(self, invocation: StageInvocation) -> StageResult: ...


StageExecutorResolver = Callable[[ExecutionBinding], StageExecutor]


@dataclass(frozen=True, slots=True)
class ContextualExecutor:
    binding: ExecutionBinding
    declaration: WorkflowStateSpec
    workspace: WorkWorkspace
    stage_executor: StageExecutor
    max_context_bytes: int = 65536

    def __post_init__(self):
        _require(type(self.max_context_bytes) is int and self.max_context_bytes > 0,
                 'max_context_bytes must be a positive integer')

    def _context(self, request):
        if any(spec.source == 'request' for spec in self.workspace.artifacts.values()):
            _require(self.workspace.read_artifact('request') == request.work_unit.task,
                     'request/WorkUnit.task mismatch')
        snapshots = []
        for name in self.declaration.consumes:
            try:
                record = self.workspace.latest_artifact(name)
                _require(record is not None, 'missing consumed artifact')
                content = self.workspace.read_artifact(name, record.version)
                data = content.encode('utf-8')
                _require(len(data) == record.byte_size and hashlib.sha256(data).hexdigest() == record.sha256,
                         'snapshot integrity mismatch')
                snapshots.append(ArtifactSnapshot(record, content))
            except WorkContractError as exc:
                raise WorkContractError(f'artifact {name}: {exc}') from exc
        previous = None
        if self.binding.context_mode == 'continue':
            matching = [h for h in self.workspace.handoffs(self.binding.logical_state)
                        if h.flow == self.binding.flow_name and h.rung_index == self.binding.rung_index - 1]
            _require(bool(matching), 'missing continuation handoff for preceding rung')
            previous = matching[-1]
        context = StageInvocation(self.binding, tuple(snapshots), previous, request.diagnostics)
        # Canonical size accounting only, not a provider prompt format.
        size = len(json.dumps(asdict(context), ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), default=lambda b: b.decode('utf-8')).encode('utf-8'))
        _require(size <= self.max_context_bytes, f'context exceeds {self.max_context_bytes} bytes')
        return context

    def run(self, request: InvocationRequest) -> InvocationResponse:
        try:
            return self._run(request)
        except WorkContractError as exc:
            raise WorkContractError(f'{self.binding.flow_name}.{self.binding.logical_state}'
                                    f'[{self.binding.rung_index}]: {exc}') from exc

    def _run(self, request):
        result = self.stage_executor.run(self._context(request))
        _require(type(result) is StageResult, 'expected StageResult')
        _require(type(result.outcome) is str and bool(result.outcome) and '\0' not in result.outcome
                 and result.outcome != 'budget_exceeded', 'invalid stage outcome')
        _require(result.diagnostics is None or type(result.diagnostics) is DiagnosticPayload,
                 'expected DiagnosticPayload')
        producer = ArtifactProducer(self.binding.flow_name, self.binding.logical_state,
                                    self.binding.rung_index, self.binding.profile_name)
        names = []
        for artifact in result.produced_artifacts:
            _require(type(artifact) is ProducedArtifact, 'expected ProducedArtifact')
            _require(type(artifact.name) is str and artifact.name in self.declaration.produces,
                     f'undeclared production: {artifact.name}')
            _require(artifact.name not in names, f'duplicate production: {artifact.name}')
            try:
                self.workspace.validate_artifact(artifact.name, artifact.content, producer)
            except WorkContractError as exc:
                raise WorkContractError(f'artifact {artifact.name}: {exc}') from exc
            names.append(artifact.name)
        handoff = None
        if result.handoff is not None:
            _require(type(result.handoff) is StageHandoff, 'expected StageHandoff body')
            handoff = HandoffRecord(flow=self.binding.flow_name, logical_state=self.binding.logical_state,
                                    role=self.binding.role_name, rung_index=self.binding.rung_index,
                                    profile_name=self.binding.profile_name, outcome=result.outcome,
                                    produced_artifacts=tuple(names), **asdict(result.handoff))
            self.workspace.validate_handoff(handoff)
        for artifact in result.produced_artifacts:
            self.workspace.write_artifact(artifact.name, artifact.content, producer)
        if handoff is not None:
            self.workspace.write_handoff(handoff)
        return InvocationResponse(ExecutorResult(result.outcome), result.diagnostics)


def workspace_resolver(bundle: WorkflowBundle, flow_name: str, workspace: WorkWorkspace,
                       stage_executor_resolver: StageExecutorResolver, *, max_context_bytes: int = 65536):
    """Compose with compile_workflow; resolve each stage once during compilation."""
    flow = bundle.flow(flow_name)
    _require(workspace.artifacts == bundle.artifacts, 'workspace catalog differs from bundle')

    def resolve(binding: ExecutionBinding):
        _require(binding.flow_name == flow_name and binding.logical_state in flow.states,
                 'binding does not belong to selected flow')
        declaration = flow.states[binding.logical_state]
        _require(binding.role_name == declaration.role, 'binding role differs from state')
        stage = stage_executor_resolver(binding)
        _require(callable(getattr(stage, 'run', None)), 'resolver must return StageExecutor')
        return ContextualExecutor(binding, declaration, workspace, stage, max_context_bytes)
    return resolve
