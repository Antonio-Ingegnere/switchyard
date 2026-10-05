"""Opt-in structured durable context transport; control flow stays in runtime."""
from dataclasses import asdict, dataclass, field, fields
import hashlib
import json
from typing import Callable, Protocol

from runtime import (DiagnosticInput, DiagnosticPayload, ExecutorResult,
                     InvocationRequest, InvocationResponse)
from .workflow_compiler import ExecutionBinding
from .work_contract import (ArtifactProducer, ArtifactRecord, HandoffRecord,
                            WorkContractError, WorkWorkspace, MicroSpecField, MICROSPEC_FIELDS,
                            HANDOFF_BODY_FIELDS, handoff_check, _kind)
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
        for descriptor in HANDOFF_BODY_FIELDS:
            if descriptor.sequence:
                value = getattr(self, descriptor.name)
                handoff_check(type(value) in (tuple, list), descriptor.name, 'sequence', _kind(value))
                object.__setattr__(self, descriptor.name, tuple(value))


if tuple(f.name for f in HANDOFF_BODY_FIELDS) != tuple(f.name for f in fields(StageHandoff)):
    raise RuntimeError('handoff body definitions drifted from StageHandoff')


@dataclass(frozen=True, slots=True)
class StageOutputArtifactContract:
    name: str
    format: str
    schema: str | None = None
    schema_fields: tuple[MicroSpecField, ...] = ()

    def __post_init__(self):
        _require(type(self.name) is str and bool(self.name), 'invalid output artifact name')
        _require(self.format in ('markdown', 'text', 'yaml', 'json'), 'invalid output artifact format')
        _require(self.schema in (None, 'microspec'), 'invalid output artifact schema')
        _tuples(self, 'schema_fields')
        _require(all(type(item) is MicroSpecField for item in self.schema_fields), 'invalid schema fields')


@dataclass(frozen=True, slots=True)
class StageHandoffContract:
    expected: bool
    evidence_artifacts: tuple[str, ...]
    supported: bool = True
    body_fields: tuple[str, ...] = field(default_factory=lambda: tuple(f.name for f in fields(StageHandoff)), init=False)

    def __post_init__(self):
        _require(type(self.expected) is bool and type(self.supported) is bool, 'invalid handoff policy')
        _tuples(self, 'evidence_artifacts')
        _require(all(type(v) is str and v for v in self.evidence_artifacts), 'invalid evidence names')


MAX_OUTPUT_CONTRACT_BYTES = 65536


@dataclass(frozen=True, slots=True)
class StageOutputContract:
    allowed_outcomes: tuple[str, ...]
    artifacts: tuple[StageOutputArtifactContract, ...]
    handoff: StageHandoffContract

    def __post_init__(self):
        _tuples(self, 'allowed_outcomes', 'artifacts')
        _require(bool(self.allowed_outcomes), 'allowed outcomes must not be empty')
        _require(all(type(v) is str and v and '\0' not in v and v != 'budget_exceeded'
                     for v in self.allowed_outcomes), 'invalid allowed semantic outcome')
        _require(all(type(v) is StageOutputArtifactContract for v in self.artifacts), 'invalid output artifact descriptor')
        _require(type(self.handoff) is StageHandoffContract, 'invalid handoff contract')
        size = len(json.dumps(asdict(self), ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')).encode('utf-8'))
        _require(size <= MAX_OUTPUT_CONTRACT_BYTES, 'output contract exceeds byte limit')


@dataclass(frozen=True, slots=True)
class StageInvocation:
    binding: ExecutionBinding
    artifacts: tuple[ArtifactSnapshot, ...]
    previous_handoff: HandoffRecord | None = None
    diagnostics: DiagnosticInput | None = None
    output_contract: StageOutputContract = field(kw_only=True)

    def __post_init__(self):
        _tuples(self, 'artifacts')
        _require(type(self.output_contract) is StageOutputContract, 'expected StageOutputContract')


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
    output_contract: StageOutputContract
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
        context = StageInvocation(self.binding, tuple(snapshots), previous, request.diagnostics,
                                  output_contract=self.output_contract)
        # Canonical size accounting only, not a provider prompt format.
        input_data = asdict(context)
        del input_data['output_contract']
        del input_data['binding']['executor_allowed_outcomes']
        size = len(json.dumps(input_data, ensure_ascii=False, sort_keys=True,
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
            handoff_check(type(result.handoff) is StageHandoff, '', 'StageHandoff body', _kind(result.handoff))
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
        contract = StageOutputContract(
            binding.executor_allowed_outcomes,
            tuple(StageOutputArtifactContract(name, bundle.artifacts[name].format, bundle.artifacts[name].schema,
                  MICROSPEC_FIELDS if bundle.artifacts[name].schema == 'microspec' else ())
                  for name in declaration.produces),
            StageHandoffContract(binding.provider != 'command', tuple(sorted(bundle.artifacts))))
        return ContextualExecutor(binding, declaration, workspace, stage, contract, max_context_bytes)
    return resolve
