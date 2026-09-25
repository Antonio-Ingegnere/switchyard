"""Claude Code CLI adapter; technical success is not task acceptance."""

from dataclasses import dataclass

from . import ExecutorResult, InvocationRequest, InvocationResponse
from .command import Command, CommandExecutor


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    claude_executable: str
    workspace: str
    invocation_timeout_seconds: float

    def __post_init__(self):
        Command(self.claude_executable, (), self.workspace, self.invocation_timeout_seconds)


@dataclass(frozen=True, slots=True)
class ClaudeExecutor:
    config: ClaudeConfig

    def __post_init__(self):
        if type(self.config) is not ClaudeConfig:
            raise TypeError("ClaudeExecutor requires ClaudeConfig")

    def run(self, request: InvocationRequest) -> InvocationResponse:
        if type(request) is not InvocationRequest:
            raise TypeError("expected InvocationRequest")
        if request.diagnostics is not None:
            raise ValueError("this executor does not consume diagnostics")
        command = Command(
            self.config.claude_executable,
            ("--print", "--no-session-persistence", "--output-format", "text",
             "--restricted", "--permission-mode", "acceptEdits",
             "--permission-prompts", "none", "--tools", "Read,Edit,Write,Glob,Grep", "--disallowedTools", "mcp__*",
             "--", "WorkUnit task:\n" + request.work_unit.task),
            self.config.workspace,
            self.config.invocation_timeout_seconds,
        )
        response = CommandExecutor(command).run(request)
        return InvocationResponse(ExecutorResult("error")) if response.result.outcome == "failed" else response
