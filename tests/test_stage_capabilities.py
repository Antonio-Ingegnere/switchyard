"""Exact capability grants; all external processes here are fake providers."""
from dataclasses import replace, FrozenInstanceError
import unittest
from unittest.mock import patch
import test_provider_stages as support
from test_workflow_compiler import fixture, FakeResolver
import test_workflow_compiler as compiler_support
from flow_config import FlowConfigError
from switchyard.workflow_compiler import compile_workflow, WorkflowCompileError
from switchyard.provider_stages import ProviderStageExecutorResolver, ProviderResolutionError, render_invocation
from switchyard.workflow_bundle import ExecutionProfile


class StageCapabilityTests(unittest.TestCase):
    setUp = support.ProviderStageTests.setUp
    calls = support.ProviderStageTests.calls
    binding = support.ProviderStageTests.binding
    adapter = support.ProviderStageTests.adapter
    load = compiler_support.WorkflowCompilerTests.load

    def test_loader_strictness_and_command_terminal_rejection(self):
        for value in ('repo_read', None, [True], [1], ['unknown'], ['repo_read','repo_read'], ['repo_write'], ['command'], ['repo_write','command']):
            with self.subTest(value=value):
                data=fixture(); data['flows']['test']['states']['A']['capabilities']=value
                with self.assertRaises(FlowConfigError): self.load(data)
        for state in ('DONE','A'):
            data=fixture(); states=data['flows']['test']['states']
            if state=='A': states['A']={'role':'command','on':{'ready':'DONE'}}
            states[state]['capabilities']=[]
            with self.assertRaises(FlowConfigError): self.load(data)

    def test_omitted_empty_order_and_shared_rungs(self):
        data=fixture()
        self.assertIsNone(self.load(data).flow('test').states['A'].capabilities)
        data['flows']['test']['states']['A']['capabilities']=[]
        self.assertEqual(self.load(data).flow('test').states['A'].capabilities, ())
        plans=[]
        for caps in (['command','repo_write','repo_read'],['repo_read','repo_write','command']):
            data['flows']['test']['states']['A']['capabilities']=caps
            compiled=compile_workflow(self.load(data),'test',FakeResolver())
            bindings=[p.binding for p in compiled.plan.values() if p.binding]
            self.assertEqual([b.capabilities for b in bindings], [('repo_read','repo_write','command')]*3)
            with self.assertRaises(FrozenInstanceError): bindings[0].capabilities=()
            plans.append(compiled.plan)
        self.assertEqual(plans[0],plans[1])

    def invoke(self, provider, caps):
        adapter,inv=self.adapter(provider, 'sonnet' if provider=='claude' else 'codex')
        binding=replace(inv.binding,capabilities=caps)
        inv=replace(inv,binding=binding)
        adapter=ProviderStageExecutorResolver({'profile':ExecutionProfile(provider,binding.model)},self.config)(binding)
        self.assertEqual(adapter.run(inv).outcome,'ready')
        return self.calls()[-1],inv

    def test_claude_exact_tools_and_permissions(self):
        for caps, tools in [([],''), (['repo_read'],'Read,Glob,Grep'),
                            (['repo_read','repo_write'],'Read,Glob,Grep,Edit,Write'),
                            (['repo_read','command'],'Read,Glob,Grep,Bash'),
                            (['repo_read','repo_write','command'],'Read,Glob,Grep,Edit,Write,Bash')]:
            call,inv=self.invoke('claude',caps);argv=call['argv']
            self.assertEqual(argv[argv.index('--tools')+1],tools)
            if tools: self.assertEqual(argv[argv.index('--allowedTools')+1],tools)
            else: self.assertNotIn('--allowedTools',argv)
            self.assertIn('--restricted',argv);self.assertIn('--strict-mcp-config',argv)
            self.assertEqual(argv[argv.index('--mcp-config')+1],'{"mcpServers":{}}')
            self.assertIn('AVAILABLE EXECUTION CAPABILITIES',call['input'])
            if not caps: self.assertIn('None. Work only from the supplied durable context.',render_invocation(inv))
        call,_=self.invoke('claude',None)
        self.assertEqual(call['argv'][call['argv'].index('--tools')+1],'Read,Edit,Write,Glob,Grep')
        self.assertNotIn('AVAILABLE EXECUTION CAPABILITIES',call['input'])

    def test_codex_matrix_and_no_tools_structured_result(self):
        for caps,sandbox,shell in [((), 'read-only','false'),(('repo_read','command'),'read-only','true'),
                                   (('repo_read','repo_write','command'),'workspace-write','true')]:
            call,_=self.invoke('codex',caps);argv=call['argv']
            self.assertEqual(argv[argv.index('--sandbox')+1],sandbox)
            self.assertIn('features.shell_tool='+shell,argv)
            self.assertIn('--output-schema',argv)
        for caps in [('repo_read',),('command',),('repo_write',),('repo_read','repo_write'),('repo_write','command')]:
            with patch('switchyard.provider_stages.execute') as execute:
                with self.assertRaisesRegex(ProviderResolutionError,'test.A rung 0: provider codex cannot represent'):
                    self.invoke('codex',caps)
                execute.assert_not_called()
        call,_=self.invoke('codex',None)
        self.assertEqual(call['argv'][call['argv'].index('--sandbox')+1],'workspace-write')
        self.assertFalse(any('features.shell_tool=' in arg for arg in call['argv']))

    def test_eager_unsupported_codex_rung(self):
        data=fixture(); data['flows']['test']['states']['A']['capabilities']=['repo_read']
        bundle=self.load(data)
        resolver=ProviderStageExecutorResolver(bundle.profiles,self.config)
        with patch('switchyard.provider_stages.execute') as execute:
            with self.assertRaises(WorkflowCompileError) as caught:
                compile_workflow(bundle,'test',resolver)
            self.assertIsInstance(caught.exception.__cause__,ProviderResolutionError)
            self.assertIn('test.A rung 2: provider codex',str(caught.exception.__cause__))
            execute.assert_not_called()
        data['flows']['test']['states']['A']['capabilities']=['repo_read','command']
        bundle=self.load(data)
        self.assertEqual(len(compile_workflow(bundle,'test',ProviderStageExecutorResolver(bundle.profiles,self.config)).executors),3)

    def test_canonical_least_privilege(self):
        compiled=compile_workflow(self.bundle,'bugfix',ProviderStageExecutorResolver(self.bundle.profiles,self.config))
        expected={'REPRODUCE':('repo_read','command'), 'FIX':('repo_read','repo_write','command'), 'TRIAGE':('repo_read',)}
        for state,caps in expected.items():
            key=compiled.entry_states[state];binding=compiled.plan[key].binding
            self.assertEqual(binding.capabilities,caps)
            from switchyard.context_transport import StageInvocation, StageOutputContract, StageHandoffContract
            invocation=StageInvocation(binding,(),output_contract=StageOutputContract(binding.executor_allowed_outcomes,(),StageHandoffContract(True,())))
            compiled.executors[key].run(invocation)
            tools=self.calls()[-1]['argv'];tools=tools[tools.index('--tools')+1].split(',')
            self.assertEqual('Bash' in tools,'command' in caps)
            self.assertEqual('Edit' in tools,'repo_write' in caps)
            self.assertEqual('Write' in tools,'repo_write' in caps)
