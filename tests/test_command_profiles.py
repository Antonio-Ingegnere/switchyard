"""V2 command capabilities are data only; no command process is executed."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import yaml
from flow_config import FlowConfigError, _yaml_data
from switchyard.workflow_bundle import load_workflow_bundle, _bundle_yaml_data
from switchyard.workflow_compiler import compile_workflow
from test_workflow_bundle import minimal, EXAMPLE
from test_workflow_compiler import FakeResolver


def fixture():
    data = minimal()
    data['artifacts'] = {'request': {'format':'text','source':'request'}, 'evidence': {'format':'text'}}
    command = data['profiles']['cmd']['command']
    command['outcomes'] = {'exit_codes': {0:'pass',2:'blocked'}, 'default':'fail'}
    command['capture'] = {'artifact':'evidence','stream':'combined','max_bytes':32768}
    state = data['flows']['check']['states']['VERIFY']
    state['on'].update(fail='DONE',blocked='DONE')
    state['produces'] = ['evidence']
    return data


class CommandProfilesTests(unittest.TestCase):
    def load(self, data):
        with TemporaryDirectory() as root:
            path=Path(root)/'bundle.yaml'; path.write_text(yaml.safe_dump(data))
            return load_workflow_bundle(path)

    def invalid(self, data, path):
        with self.assertRaises(FlowConfigError) as caught:
            self.load(data)
        self.assertIn(path,str(caught.exception))

    def test_valid_and_immutable_compiled_profiles(self):
        with patch('subprocess.Popen') as process:
            bundle=self.load(fixture())
            compiled=compile_workflow(bundle,'check',FakeResolver())
        process.assert_not_called()
        command=compiled.profiles['cmd'].command
        self.assertEqual(command.argv,('python3','-m','unittest'))
        self.assertEqual(dict(command.outcomes.exit_codes),{0:'pass',2:'blocked'})
        self.assertEqual(command.outcomes.default,'fail')
        self.assertEqual(command.capture.artifact,'evidence')
        with self.assertRaises(TypeError): command.outcomes.exit_codes[0]='other'
        with self.assertRaises(TypeError): compiled.profiles['other']=bundle.profiles['cmd']
        with self.assertRaises(FrozenInstanceError): command.capture.max_bytes=1
        no_capture=fixture(); del no_capture['profiles']['cmd']['command']['capture']
        self.assertIsNone(self.load(no_capture).profiles['cmd'].command.capture)

    def test_invalid_argv(self):
        for value in [None, [], 'python3 test.py', [''], [True], [1], ['a\0b']]:
            with self.subTest(value=value):
                data=fixture(); data['profiles']['cmd']['command']['argv']=value
                self.invalid(data,'command.argv')
        data=fixture(); del data['profiles']['cmd']['command']['argv']
        self.invalid(data,'command.argv')

    def test_outcomes(self):
        for key in ['outcomes']:
            data=fixture(); del data['profiles']['cmd']['command'][key]
            self.invalid(data,'command.'+key)
        for field in ['exit_codes','default']:
            data=fixture(); del data['profiles']['cmd']['command']['outcomes'][field]
            self.invalid(data,'outcomes.'+field)
        for code in ['0',True,-1,1.5,None]:
            with self.subTest(code=code):
                data=fixture(); data['profiles']['cmd']['command']['outcomes']['exit_codes']={code:'pass'}
                self.invalid(data,'exit_codes')
        for value in [None,True,4,'','with space','budget_exceeded']:
            for field in ['default','exit_codes']:
                with self.subTest(value=value,field=field):
                    data=fixture(); outcomes=data['profiles']['cmd']['command']['outcomes']
                    outcomes[field]=value if field=='default' else {0:value}
                    self.invalid(data,'outcomes.'+field)
        data=fixture(); data['profiles']['cmd']['command']['outcomes']['exit_codes']={}
        self.assertEqual(dict(self.load(data).profiles['cmd'].command.outcomes.exit_codes),{})

    def test_provider_separation(self):
        data=fixture(); del data['profiles']['cmd']['command']; self.invalid(data,'profiles.cmd.command')
        for provider in ['claude','codex']:
            data=fixture(); data['profiles']['cmd'].update(provider=provider,model='model')
            self.invalid(data,'profiles.cmd.command')
        for field,value in [('model','model'),('effort','high')]:
            data=fixture(); data['profiles']['cmd'][field]=value; self.invalid(data,'profiles.cmd.'+field)
        data=fixture(); data['roles']['verify']['primary']['effort']='high'
        self.invalid(data,'primary.effort')

    def test_capture_targets(self):
        for name in ['missing','request']:
            data=fixture(); data['profiles']['cmd']['command']['capture']['artifact']=name
            self.invalid(data,'capture.artifact')
        for definition in [{'format':'yaml'},{'format':'json'},{'format':'yaml','schema':'microspec'}]:
            data=fixture(); data['artifacts']['evidence']=definition; self.invalid(data,'capture.artifact')
        data=fixture(); data['artifacts']['evidence']['format']='markdown'
        self.load(data)

    def test_capture_shape(self):
        for field in ['artifact','stream','max_bytes']:
            data=fixture(); del data['profiles']['cmd']['command']['capture'][field]
            self.invalid(data,'capture.'+field)
        for value in [0,-1,True,'32',1.1,None]:
            data=fixture(); data['profiles']['cmd']['command']['capture']['max_bytes']=value
            self.invalid(data,'capture.max_bytes')
        for value in ['both','',True]:
            data=fixture(); data['profiles']['cmd']['command']['capture']['stream']=value
            self.invalid(data,'capture.stream')
        for value in ['stdout','stderr','combined']:
            data=fixture(); data['profiles']['cmd']['command']['capture']['stream']=value
            self.load(data)

    def test_unknown_fields(self):
        for location in ['command','outcomes','capture']:
            for key in ['env','retries','commands','unknown']:
                data=fixture(); target=data['profiles']['cmd']['command']
                if location!='command': target=target[location]
                target[key]='unsupported'; self.invalid(data,key)

    def test_state_reuse_contracts(self):
        data=fixture(); flow=data['flows']['check']
        flow['states']['VERIFY_AGAIN']=deepcopy(flow['states']['VERIFY'])
        flow['states']['VERIFY']['on']['pass']='VERIFY_AGAIN'
        self.load(data)
        missing=deepcopy(data); del missing['flows']['check']['states']['VERIFY_AGAIN']['on']['fail']
        self.invalid(missing,'VERIFY_AGAIN.on')
        missing=deepcopy(data); missing['flows']['check']['states']['VERIFY_AGAIN']['produces']=[]
        self.invalid(missing,'VERIFY_AGAIN.produces')

    def test_escalation_outcome_resolution(self):
        data=fixture()
        data['profiles']['agent']={'provider':'codex','model':'codex'}
        data['roles']['verify']['escalation']=[{'kind':'horizontal','profile':'agent','context':'fresh','on':['inconclusive']}]
        state=data['flows']['check']['states']['VERIFY']; state['escalation_exhausted']='DONE'
        command=data['profiles']['cmd']['command']
        # Incoming adjacent trigger, normal on, and non-adjacent trigger exhaustion are valid.
        command['outcomes']['exit_codes']={0:'inconclusive',2:'pass',3:'diagnosis_disproved'}
        self.load(data)
        # Validate a command profile when it is a later rung, too.
        data['roles']['verify']={'primary':{'profile':'agent'},'escalation':[
            {'kind':'horizontal','profile':'cmd','context':'fresh','on':['inconclusive']}]}
        self.load(data)
        command['outcomes']['default']='undeclared'
        self.invalid(data,'states.VERIFY.on')

    def test_yaml_integer_boundary_and_duplicates(self):
        source=yaml.safe_dump(fixture())
        with self.assertRaises(FlowConfigError): _yaml_data(source)  # V1 parser unchanged.
        self.assertEqual(_bundle_yaml_data(source)['profiles']['cmd']['command']['outcomes']['exit_codes'][0],'pass')
        duplicate=source.replace('0: pass','0: pass\n          0: fail')
        with self.assertRaisesRegex(FlowConfigError,'duplicate mapping key'): _bundle_yaml_data(duplicate)
        with self.assertRaises(FlowConfigError): _bundle_yaml_data('version: 2\n0: invalid\n')
        for name in ['verify.tools','verify-tools']:
            data=fixture(); data['profiles'][name]=data['profiles'].pop('cmd'); data['roles']['verify']['primary']['profile']=name
            self.load(data)

    def test_canonical_profile(self):
        with patch('subprocess.Popen') as process:
            bundle=load_workflow_bundle(EXAMPLE)
            for name in ('feature','bugfix'):
                compiled=compile_workflow(bundle,name,FakeResolver())
                self.assertEqual(compiled.profiles['command'].command.argv,
                                 ('python3','-m','unittest','discover','-s','tests'))
                for state in ('VERIFY','VERIFY_AGAIN'):
                    self.assertIn('verifier_evidence',bundle.flow(name).states[state].produces)
        process.assert_not_called()


if __name__=='__main__': unittest.main()
