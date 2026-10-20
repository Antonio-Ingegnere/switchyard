"""Read pre-TERMINAL-CONTRACT frozen work; never used to start a new delivery."""
from flow_config import _yaml_data, _error, _fields, _mapping
from .simple_config import _simple_config_from_data


def read_legacy_project(text):
    """Reproduce the unversioned CONFIG-001 contract without rewriting its bytes."""
    data = _yaml_data(text)
    _fields(data, {'agents', 'flows'}, {'lifecycle'}, '')
    _mapping(data['flows'], 'flows')
    for flow_name, flow in data['flows'].items():
        path = f'flows.{flow_name}'
        _fields(flow, {'start', 'states'}, {'limits'}, path)
        states = flow['states']
        _mapping(states, path+'.states')
        targets = [flow.get('limits', {}).get('on_exceeded', 'NEEDS_HUMAN')]
        for name, state in tuple(states.items()):
            _mapping(state, path+'.states.'+name)
            if 'terminal' in state:
                if 'resume_to' in state and name not in ('NEEDS_HUMAN', 'NEEDS_REDIAGNOSIS'):
                    raise _error(path+'.states.'+name, 'legacy terminal cannot declare recovery')
                continue
            if 'wait_for' in state:
                targets.append(state.get('budget', {}).get('on_exceeded'))
                continue
            _mapping(state.get('on', {}), path+'.states.'+name+'.on')
            targets.extend(state.get('on', {}).values())
            targets.append(state.get('budget', {}).get('on_exceeded', 'NEEDS_HUMAN'))
            if len(state.get('run', [])) > 1:
                targets.append(state.get('on_exhausted', 'NEEDS_HUMAN'))
        for target in targets:
            if target in ('READY_FOR_MANUAL_QA', 'NEEDS_HUMAN', 'NEEDS_REDIAGNOSIS'):
                states.setdefault(target, {'terminal': True})
    return _simple_config_from_data(data)
