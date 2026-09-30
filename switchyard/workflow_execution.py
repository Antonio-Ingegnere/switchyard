"""Opt-in application composition; control flow remains owned by runtime.run."""
from dataclasses import dataclass
from pathlib import Path

from runtime import EventRecorder, RunResult, WorkUnit, run
from .context_transport import workspace_resolver
from .provider_stages import ProviderRuntimeConfig, ProviderStageExecutorResolver
from .work_contract import WorkWorkspace
from .workflow_bundle import WorkflowBundle
from .workflow_compiler import CompiledWorkflow, compile_workflow


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    result: RunResult
    workspace: WorkWorkspace
    compiled: CompiledWorkflow


def execute_v2_workflow(bundle: WorkflowBundle, flow_name: str, request: str,
                        work_root: str | Path, provider_runtime_config: ProviderRuntimeConfig,
                        *, recorder: EventRecorder | None = None) -> WorkflowExecution:
    workspace = WorkWorkspace.open(work_root, bundle.artifacts)
    workspace.initialize_request(request)
    provider = ProviderStageExecutorResolver(bundle.profiles, provider_runtime_config)
    resolver = workspace_resolver(bundle, flow_name, workspace, provider)
    compiled = compile_workflow(bundle, flow_name, resolver)
    result = run(WorkUnit(request), compiled.flow, compiled.executors, recorder=recorder)
    return WorkflowExecution(result, workspace, compiled)
