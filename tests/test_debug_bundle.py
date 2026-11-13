"""OBSERVABILITY (#1) slice 4: sanitized debug bundle. Real offline deliveries; every test inspects the ZIP itself."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import zipfile

from switchyard import bundle
from switchyard.bundle import BundleError, FILES, create_bundle
from switchyard.cli import main
from switchyard.diagnostics import diagnostic_path, read_diagnostics
from switchyard.recovery import _lock, accept_v2_candidate, recovery_status
from switchyard.trace import trace_events

import test_human_input
from test_provider_diagnostics import DiagnosticsBase, SECRET


class BundleBase(DiagnosticsBase):
    def make(self, name='b.zip', work=None):
        path = self.root/name
        create_bundle(work or self.work, path)
        return path

    def open(self, path):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            data = {i.filename: archive.read(i.filename) for i in infos}
        manifest = json.loads(data['manifest.json'])
        lines = lambda name: [json.loads(l) for l in data[name].decode('ascii').splitlines()]
        return dict(infos=infos, data=data, manifest=manifest, trace=lines('trace.jsonl'), diagnostics=lines('diagnostics.jsonl'),
                    runtime=json.loads(data['runtime.json']), status=json.loads(data['status.json']))

    def aliases(self, work=None):
        """Test-side recomputation from the SOURCE work (a bundle recipient has no access to the key material)."""
        return bundle._Aliases(bundle._alias_key((work or self.work).resolve()))

    def blob(self, z):
        return b''.join(z['data'].values())

    def work_bytes(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else os.readlink(p) if p.is_symlink() else None)
                for p in sorted(self.work.rglob('*'))}


class NormalBundleTests(BundleBase):
    # 1, 2, 3, 4, 23, 25, 26
    def test_a_delivery_bundle_is_structured_correlated_deterministic_and_read_only(self):
        self.start(); calls = len(self.calls())
        before = self.work_bytes()
        def forbidden(*a, **k):
            raise AssertionError('no subprocess, Git, provider or network during export')
        with patch('subprocess.Popen', forbidden), patch('socket.socket', forbidden):
            first, second = self.make('one.zip'), self.make('two.zip')
        self.assertEqual(first.read_bytes(), second.read_bytes())  # 23: byte-identical for unchanged source data.
        self.assertEqual((self.work_bytes(), len(self.calls())), (before, calls))  # 25, 26
        z = self.open(first)
        self.assertEqual([i.filename for i in z['infos']], list(FILES))
        for info in z['infos']:  # Fixed, normalized, regular-file entries only.
            self.assertEqual((info.date_time, info.external_attr >> 16, info.create_system), ((1980, 1, 1, 0, 0, 0), 0o100644, 3))
            self.assertFalse(info.filename.startswith('/') or '..' in info.filename or '/' in info.filename)
        m = z['manifest']
        self.assertEqual((m['bundle_schema_version'], m['completeness']), (1, dict(trace='complete', diagnostics='complete', history='complete')))
        self.assertEqual({f['path']: (f['sha256'], f['size_bytes']) for f in m['files']},
                         {n: (hashlib.sha256(z['data'][n]).hexdigest(), len(z['data'][n])) for n in FILES[1:]})
        trace, a = trace_events(self.work), self.aliases()
        self.assertEqual([(e['sequence'], e['event_id'], e['run_id'], e['state_visit_id'], e['invocation_id']) for e in z['trace']],
                         [(e['sequence'], a.rekey('eid', e['event_id']), e['run_id'], e['state_visit_id'], e['invocation_id']) for e in trace])
        self.assertEqual([e['state'] for e in z['trace']], [e['state'] and a.name('state', e['state']) for e in trace])
        self.assertEqual((m['counts']['events'], m['counts']['diagnostics'], m['counts']['canonical_events']), (22, 3, len(self.canonical())))
        started = [e['invocation_id'] for e in z['trace'] if e['event_type'] == 'invocation_started']
        self.assertEqual([d['invocation_id'] for d in z['diagnostics']], started)
        linked = {e['invocation_id']: e['payload']['diagnostic_id'] for e in z['trace'] if e['event_type'] == 'invocation_finished'}
        self.assertEqual(linked, {d['invocation_id']: d['diagnostic_id'] for d in z['diagnostics']})
        finished = [e for e in z['trace'] if e['event_type'] == 'invocation_finished'][0]['payload']
        self.assertEqual((finished['outcome'], type(finished['duration_ms'])), (a.name('outcome', 'ready'), int))  # User outcome.
        budget = [e for e in z['trace'] if e['event_type'] == 'invocation_started'][0]['payload']['budget']
        self.assertEqual(budget['run_invocations'], dict(used_before=0, used_after=1, limit=12))
        r = z['runtime']
        baseline = json.loads((self.work/'recovery').joinpath(
            [json.loads(l) for l in (self.work/'recovery/manifest.jsonl').read_text().splitlines() if '"baseline"' in l][-1]['path']).read_text())
        self.assertEqual((r['baseline']['config_sha256'], r['baseline']['work_schema_version'], r['baseline']['flow'], r['git']['base_sha']),
                         (hashlib.sha256(baseline['config'].encode()).hexdigest(), 3, a.name('flow', 'feature'), self.base))
        self.assertEqual((r['git']['delivery_branch'], r['exporter']['switchyard_version'], r['integration'], m['work_id']),
                         ('refs/heads/switchyard/'+a.name('work', 'work'), m['switchyard_version'], 'not_applicable', a.name('work', 'work')))
        self.assertEqual((z['status']['status'], z['status']['active']), ('READY_FOR_MANUAL_QA', False))
        code, out, _ = self.run_cli('debug-bundle', str(self.work), '--output', str(self.root/'three.zip'), '--json')
        summary = json.loads(out)
        self.assertEqual((code, summary['sha256'], summary['counts']['events']), (0, hashlib.sha256(first.read_bytes()).hexdigest(), 22))
        self.assertNotIn('trace', summary)  # Counts, hashes and completeness only; never the records.
        code, out, _ = self.run_cli('debug-bundle', str(self.work), '--output', str(self.root/'four.zip'))
        self.assertIn('Trace completeness:  complete', out); self.assertIn('Secrets:             not intentionally included', out)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['work', *args])
        return code, out.getvalue(), err.getvalue()

    def test_an_accepted_work_exports_its_accepted_commit(self):
        execution = self.start(); self.candidate(execution)
        accept_v2_candidate(self.work)
        z = self.open(self.make())
        self.assertEqual(z['status']['status'], 'ACCEPTED')
        self.assertEqual(z['status']['accepted_commit_sha'], recovery_status(self.work)['accepted_commit_sha'])


class FailureBundleTests(BundleBase):
    # 5
    def test_a_provider_failure_is_identifiable_and_explained_by_codes(self):
        self.interrupted()
        z = self.open(self.make())
        stop = z['trace'][-1]
        self.assertEqual((stop['event_type'], stop['state'], stop['payload']['exit_code'], stop['payload']['outcome_category']),
                         ('run_interrupted', self.aliases().name('state', 'IMPLEMENT'), 7, 'provider_exit'))
        self.assertIn('payload.diagnostic', stop['excluded_for_safety'])  # The free-text diagnostic stays local.
        d = [d for d in z['diagnostics'] if d['invocation_id'] == stop['invocation_id']][0]
        self.assertEqual((d['result']['classification'], d['result']['reason'], d['process']['exit_code'], d['diagnostic_id']),
                         ('provider_exit', 'provider_failure', 7, stop['payload']['diagnostic_id']))
        self.assertEqual(set(d['excluded_for_safety']), {'summary', 'result.error'})
        self.assertNotIn('summary', d); self.assertNotIn('error', d['result'])
        self.assertEqual((z['status']['status'], z['status']['reason'], z['status']['resume_safe']), ('INTERRUPTED', 'provider_failure', True))

    # 6, 7, 8
    def test_a_structured_output_failure_exports_path_and_category_only(self):
        result = dict(outcome='ready', produced_artifacts=[dict(name='implementation_handoff', content='RAW_SOURCE def f(): return 1',
                                                                bogus_0='RAW_VALUE')], handoff=None, diagnostics=None)
        envelope = json.dumps(dict(type='result', subtype='success', is_error=False, structured_output=result)).encode()
        self.deliver(self.claude_emits(envelope, b'Traceback: billing.py line 3: rate = 0.42\n'), fails=True)
        z = self.open(self.make())
        a = self.aliases()
        d = [d for d in z['diagnostics'] if d['state'] == a.name('state', 'IMPLEMENT')][0]
        s = d['structured_output']
        self.assertEqual((s['valid'], s['raw_result_retained'], s['errors']),
                         (False, False, [dict(path='produced_artifacts[0].'+a.alias('field', 'bogus_0'), category='additional_property')]))
        self.assertNotIn(b'bogus_0', self.blob(z))  # A foreign field name is itself pseudonymized.
        self.assertEqual((d['stderr_excerpt']['available'], d['stderr_excerpt']['reason']), (False, 'unstructured_output_not_retained'))
        for leaked in (b'RAW_SOURCE', b'RAW_VALUE', b'def f()', b'billing.py', b'0.42', b'allowed_fields'):
            self.assertNotIn(leaked, self.blob(z))


class SafetyTests(BundleBase):
    # 7, 8, 9, 10, 11
    def test_no_payloads_prompts_source_environment_credentials_or_paths(self):
        config = self.claude_emits(json.dumps(dict(type='result', subtype='error_during_execution', is_error=True,
                                                   errors=['auth failed for token '+SECRET])).encode(),
                                   ('Authorization: Bearer '+SECRET+'\n').encode(), exit_code=1)
        config = __import__('dataclasses').replace(config, environment={**config.environment, 'GH_TOKEN': SECRET})
        self.deliver(config, fails=True)
        blob = self.blob(self.open(self.make()))
        for leaked in (SECRET, 'Authorization', 'STAGE OUTPUT CONTRACT', 'EXECUTION BINDING', 'Change source.py',
                       'operational', 'progress:', str(self.root), str(self.target), '/Users/', '/private/', 'GH_TOKEN',
                       'FAKE_PLAN', '"agents"', sys.executable, 'microspec content'):
            self.assertNotIn(leaked.encode(), blob, leaked)

    # 12, 13
    def test_hostile_values_and_unknown_fields_in_source_records_are_not_exported(self):
        self.interrupted()
        path = diagnostic_path(self.work, read_diagnostics(self.work, 'I2')[0]['invocation_id'])
        record = json.loads(path.read_text())
        record['agent'] = '/Users/victim/.ssh/id_rsa'
        record['result']['outcome'] = '../../etc/passwd'
        record['result']['classification'] = 'Bearer '+SECRET
        record['summary'] = 'password='+SECRET
        record['leak'] = {'token': SECRET}
        record['structured_output']['errors'] = [dict(path='x.$(curl evil)', error='additional property is not allowed', allowed_fields=['k'])]
        path.write_text(json.dumps(record))
        z = self.open(self.make())
        d = [d for d in z['diagnostics'] if d['invocation_id'] == record['invocation_id']][0]
        self.assertEqual((d['agent'], d['result']['outcome'], d['result']['classification']), (None, None, None))
        self.assertLessEqual({'agent', 'result.outcome', 'result.classification', 'summary'}, set(d['excluded_for_safety']))
        self.assertEqual(d['structured_output']['errors'], [dict(path=None, category='additional_property')])
        self.assertNotIn('leak', d)
        self.assertGreaterEqual(z['manifest']['counts']['unrecognized_fields_dropped'], 1)
        for leaked in (SECRET, 'id_rsa', 'passwd', 'curl evil'):
            self.assertNotIn(leaked.encode(), self.blob(z))

    # 20
    def test_symlinks_and_hostile_names_never_reach_outside_or_into_the_archive(self):
        self.start()
        outside = self.root/'outside'; outside.mkdir(); (outside/'secret.txt').write_text('OUTSIDE_SECRET')
        events = self.work/'observability/events'
        name = sorted(n for n in os.listdir(events) if n.endswith('.json'))[3]
        (events/name).unlink(); (events/name).symlink_to(outside/'secret.txt')
        (events/'../../escape.json').resolve()  # (A name cannot contain '/'; hostile names are just unexpected entries.)
        (events/'000099-$(touch pwned).json').write_text('{}')
        diagnostics = self.work/'observability/diagnostics'
        (diagnostics/'..evil.json').write_text(json.dumps(dict(schema_version=1, invocation_id='OUTSIDE_SECRET')))
        z = self.open(self.make())
        self.assertEqual([i.filename for i in z['infos']], list(FILES))
        self.assertNotIn(b'OUTSIDE_SECRET', self.blob(z)); self.assertNotIn(b'pwned', self.blob(z))
        counts = z['manifest']['issue_counts']
        self.assertEqual((counts['unreadable_symlinked_or_oversized_record'], counts['unexpected_entry']), (1, 1))
        self.assertEqual(counts['record_does_not_match_its_identity'], 1)
        self.assertEqual(z['manifest']['completeness']['trace'], 'partial')
        shutil.move(str(self.work/'observability'), str(outside/'obs'))  # The store itself is a symlink.
        (self.work/'observability').symlink_to(outside/'obs')
        z = self.open(self.make('linked.zip'))
        self.assertEqual((z['manifest']['sources']['trace'], z['manifest']['completeness']['trace'], z['trace']),
                         ('excluded_for_safety', 'unavailable', []))


class CompletenessTests(BundleBase):
    # 14, 15, 18
    def test_corrupt_missing_and_unsupported_records_are_reported_never_labelled_complete(self):
        self.start()
        records = read_diagnostics(self.work)
        events = self.work/'observability/events'
        names = sorted(n for n in os.listdir(events) if n.endswith('.json'))
        (events/names[5]).write_text('{"schema_version": 1, "sequ')  # Corrupt.
        os.unlink(diagnostic_path(self.work, records[0]['invocation_id']))  # Missing.
        path = diagnostic_path(self.work, records[1]['invocation_id'])
        path.write_text(json.dumps(dict(json.loads(path.read_text()), schema_version=99)))  # Unsupported future schema.
        z = self.open(self.make())
        m = z['manifest']
        self.assertEqual(m['completeness'], dict(trace='partial', diagnostics='partial', history='partial'))
        # The unsupported record is not exported, so its invocation also lacks a usable diagnostic: 2 missing.
        self.assertEqual((m['counts']['corrupt_records'], m['counts']['missing_diagnostics'], m['counts']['unsupported_schema_records']), (1, 2, 1))
        self.assertEqual(m['issue_counts'], {'corrupt_record': 1, 'canonical_event_not_traced': 1,
                                             'missing_diagnostic_for_finished_invocation': 2, 'missing_sequence': 1,
                                             'unsupported_schema_version': 1})
        self.assertIn(dict(area='trace', reason='corrupt_record', sequence=6), m['issues'])
        self.assertEqual(len(z['trace']), 21); self.assertEqual(len(z['diagnostics']), 1)
        self.assertEqual([d['invocation_id'] for d in z['diagnostics']], [records[2]['invocation_id']])

    # 16
    def test_an_invocation_without_a_proven_end_stays_uncertain(self):
        self.start()
        events = self.work/'observability/events'
        started = [e for e in trace_events(self.work) if e['event_type'] == 'invocation_started' and e['state'] == 'IMPLEMENT'][0]
        for name in os.listdir(events):
            if name.endswith('.json') and int(name[:6]) > started['sequence']:
                os.unlink(events/name)  # As if the controller died right after launching IMPLEMENT.
        for record in read_diagnostics(self.work)[1:]:
            os.unlink(diagnostic_path(self.work, record['invocation_id']))
        lines = (self.work/'events.jsonl').read_text().splitlines(True)
        (self.work/'events.jsonl').write_text(''.join(lines[:started['source']['index']+1]))
        z = self.open(self.make())
        m = z['manifest']
        self.assertEqual((m['completeness']['trace'], m['completeness']['diagnostics'], m['completeness']['history']),
                         ('complete', 'partial', 'partial'))
        self.assertEqual(m['counts']['invocations_without_proven_end'], 1)
        self.assertIn(dict(area='diagnostics', reason='invocation_without_proven_end', invocation_id=started['invocation_id']), m['issues'])
        self.assertEqual(z['trace'][-1]['event_type'], 'invocation_started')  # Nothing invented after it.
        self.assertFalse([d for d in z['diagnostics'] if d['invocation_id'] == started['invocation_id']])

    # 17
    def test_a_legacy_work_without_observability_exports_only_what_it_proves(self):
        self.start()
        shutil.rmtree(self.work/'observability')
        z = self.open(self.make())
        m = z['manifest']
        self.assertEqual((m['completeness'], m['sources']['trace'], m['counts']['events']),
                         (dict(trace='unavailable', diagnostics='unavailable', history='partial'), 'unavailable', 0))
        self.assertEqual((z['trace'], z['diagnostics'], z['runtime']['baseline']['work_schema_version']), ([], [], 3))
        with self.assertRaisesRegex(BundleError, 'not a Switchyard work'):
            create_bundle(self.root/'target', self.root/'x.zip')


class OutputTests(BundleBase):
    # 19, 21, 22
    def test_limits_fail_explicitly_and_no_partial_or_overwritten_output_remains(self):
        self.start()
        before = self.work_bytes()
        for limit, value, message in (('MAX_EVENTS', 5, '22 trace events > 5'), ('MAX_UNCOMPRESSED', 4096, 'uncompressed size'),
                                      ('MAX_RECORD', 200, 'exceeds 200 bytes'), ('MAX_DIAGNOSTICS', 2, '3 diagnostic records > 2')):
            with patch.object(bundle, limit, value), self.assertRaisesRegex(BundleError, message):
                self.make('limited.zip')
        with patch.object(bundle, 'MAX_COMPRESSED', 100), self.assertRaisesRegex(BundleError, 'compressed size'):
            self.make('limited.zip')
        existing = self.root/'existing.zip'; existing.write_bytes(b'keep me')
        with self.assertRaisesRegex(BundleError, 'never overwritten'):
            create_bundle(self.work, existing)
        self.assertEqual(existing.read_bytes(), b'keep me')
        original = zipfile.ZipFile.writestr
        def failing(self_, info, data, *a, **k):
            if getattr(info, 'filename', info) == 'status.json':
                raise OSError('disk full')
            return original(self_, info, data, *a, **k)
        with patch.object(zipfile.ZipFile, 'writestr', failing), self.assertRaises(OSError):
            self.make('failing.zip')
        with self.assertRaisesRegex(BundleError, 'inside the work'):
            create_bundle(self.work, self.work/'workspace'/'b.zip')
        with self.assertRaisesRegex(BundleError, 'does not exist'):
            create_bundle(self.work, self.root/'missing-dir'/'b.zip')
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.endswith(('.zip', '.partial'))), ['existing.zip'])
        self.assertEqual(self.work_bytes(), before)

    # 21 (race): a destination created after the checks is never clobbered.
    def test_a_destination_created_during_export_is_not_clobbered(self):
        self.start()
        target = self.root/'race.zip'
        original = bundle.build
        def racing(root):
            result = original(root)
            target.write_bytes(b'theirs')
            return result
        with patch.object(bundle, 'build', racing), self.assertRaisesRegex(BundleError, 'never overwritten'):
            create_bundle(self.work, target)
        self.assertEqual(target.read_bytes(), b'theirs')
        self.assertFalse([p for p in self.root.iterdir() if p.name.endswith('.partial')])

    # 24
    def test_export_while_a_controller_holds_the_work_does_not_take_its_lock(self):
        self.interrupted()
        with _lock(self.work):  # A controller is active: export must neither block on nor take this lock.
            z = self.open(self.make())
        self.assertEqual(z['status']['status'], 'INTERRUPTED')


class PseudonymTests(BundleBase):
    """User-chosen names that pass identifier validation but look confidential never leave the work."""
    CONFIDENTIAL = (b'acme', b'titan', b'cfo', b'merger', b'term_sheet', b'audit_log', b'secret')

    def setUp(self):
        super().setUp()
        self.work = self.root/'acme-merger-2027'; self.delivery = self.work/'workspace'
        tests = dict(self.data['agents']['tests'], capture='acme_audit_log')
        self.data['agents'] = {'acme_analyst': dict(self.data['agents']['spec'], model='acme-private-llm'),
                               'acme_engineer': self.data['agents']['code'], 'acme_auditor': tests}
        self.data['flows'] = {'project_titan': {'start': 'ACQUIRE_ACME', 'limits': {'run_invocations': 12}, 'states': {
            'NEEDS_HUMAN': dict(terminal=True), 'ESCALATE_TO_CFO': dict(terminal=True), 'READY_FOR_MANUAL_QA': dict(terminal=True, resume_to='MERGE_ACME'),
            'ACQUIRE_ACME': dict(agent='acme_analyst', consumes=['request'],
                                 produces={'acme_term_sheet': dict(format='yaml', schema='microspec')}, on={'ready': 'MERGE_ACME'}),
            'MERGE_ACME': dict(run=[dict(agent='acme_engineer', budget={'invocations': 3})], budget={'visits': 2},
                               consumes=['acme_term_sheet'], produces={'acme_handoff': dict(format='markdown')}, on={'ready': 'AUDIT_ACME'}),
            'AUDIT_ACME': dict(agent='acme_auditor', rung_budget_scope='visit', budget={'visits': 2}, consumes=['acme_handoff'],
                               produces={'acme_audit_log': dict(format='markdown')},
                               on={'pass': 'READY_FOR_MANUAL_QA', 'fail': 'ESCALATE_TO_CFO'})}}}
        self.text = json.dumps(self.data); (self.target/'.switchyard/config.yaml').write_text(self.text)
        self.git('checkout', '-qb', 'acme/secret-merger'); self.git('commit', '-qam', 'confidential workflow')
        self.base = self.git('rev-parse', 'HEAD')
        from switchyard.recovery import execute_recoverable_v2_work
        execute_recoverable_v2_work(self.text, 'project_titan', 'Acquire ACME quietly', self.work, self.config)

    def test_user_names_are_pseudonymized_consistently_and_the_mapping_never_leaves(self):
        path = self.make()
        z = self.open(path)
        lowered = path.read_bytes().lower()
        with zipfile.ZipFile(path) as archive:
            contents = b''.join(archive.read(n) for n in archive.namelist()).lower()
        for name in self.CONFIDENTIAL:  # Neither the compressed bytes nor any decompressed entry.
            self.assertNotIn(name, contents, name); self.assertNotIn(name, lowered, name)
        m, r, trace, diags = z['manifest'], z['runtime'], z['trace'], z['diagnostics']
        import re
        alias = re.compile(r'(work|flow|state|agent|model|artifact|outcome|executable|branch)-[0-9a-f]{12}')
        self.assertRegex(m['work_id'], r'^work-[0-9a-f]{12}$')
        self.assertEqual(r['git']['delivery_branch'], 'refs/heads/switchyard/'+m['work_id'])  # Same identity, same alias.
        self.assertRegex(r['git']['source_branch'], r'^refs/heads/branch-[0-9a-f]{12}$')
        self.assertTrue(alias.fullmatch(r['baseline']['flow']))
        flow = r['baseline']['flow']
        self.assertEqual({e['work_id'] for e in trace} | {d['work_id'] for d in diags}, {m['work_id']})
        for e in trace:  # Composite runtime ids are rebuilt from the same aliases.
            if e['runtime_state']:  # Bound states are composite; unbound terminals are the bare state, same alias.
                self.assertEqual(e['runtime_state'], e['state'] if e['rung'] is None else '@v2:%s:%s:%s' % (flow, e['state'], e['rung']))
        started = {e['invocation_id']: e for e in trace if e['event_type'] == 'invocation_started'}
        for d in diags:
            s = started[d['invocation_id']]
            self.assertEqual((d['state'], d['agent'], d['provider'], d['model']), (s['state'], s['agent'], s['provider'], s['model']))
        routing = [e['payload'] for e in trace if e['event_type'] == 'routing_decided']
        self.assertEqual([p['policy_identity'] for p in routing], ['@v2:%s:%s:0' % (flow, p['requested_state']) for p in routing])
        scopes = [s['scope'] for e in started.values() for s in e['payload']['budget']['invocation_scopes']]
        self.assertTrue(all(re.fullmatch(r'@v2-scope:%s:state-[0-9a-f]{12}:(state|rung:0)' % flow, s) for s in scopes), scopes)
        transitions = [e for e in trace if e['event_type'] == 'transition_decided']
        entered = [e['state'] for e in trace if e['event_type'] == 'state_entered']
        self.assertEqual([t['payload']['to_state'] for t in transitions], entered[1:])
        audit = [d for d in diags if d['provider'] == 'command'][0]
        self.assertEqual((audit['verification']['verifier'], audit['verification']['evidence_artifact']),
                         (audit['agent'], audit['verification']['evidence_refs'][0]['name']))
        self.assertTrue(alias.fullmatch(audit['verification']['executable']))
        self.assertEqual(z['status']['terminal'], trace[-1]['payload']['terminal'])
        self.assertEqual(z['status']['status'], z['status']['terminal'])  # A user terminal is a pseudonymized state.
        # Kept: built-in providers and models, runtime vocabulary.
        self.assertEqual({d['provider'] for d in diags}, {'codex', 'claude', 'command'})
        self.assertEqual([d['model'] for d in diags if d['provider'] == 'claude'], ['sonnet'])
        self.assertTrue(alias.fullmatch([d['model'] for d in diags if d['provider'] == 'codex'][0]))
        self.assertEqual([e['event_type'] for e in trace][:3], ['run_started', 'state_entered', 'routing_decided'])
        self.assertEqual(routing[0]['capabilities'], ['repo_read', 'command'])
        # Deterministic, and independent of directory iteration order.
        real = os.listdir
        with patch('switchyard.bundle.os.listdir', lambda p: list(reversed(real(p)))):
            reordered = self.make('reordered.zip')
        self.assertEqual(reordered.read_bytes(), path.read_bytes())
        self.assertEqual(self.make('again.zip').read_bytes(), path.read_bytes())


class HumanWaitBundleTests(unittest.TestCase):
    _base = test_human_input.HumanInputTests
    setUp, setUp_base, git, save_config, start, answer = (_base.setUp, _base.setUp_base, _base.git, _base.save_config,
                                                          _base.start, _base.answer)

    def test_a_waiting_work_reports_its_request_and_never_exports_the_answer(self):
        self.start()
        target = self.work.parent/'wait.zip'
        create_bundle(self.work, target)
        with zipfile.ZipFile(target) as archive:
            status = json.loads(archive.read('status.json'))
        self.assertEqual((status['status'], status['request_id']), ('WAITING_FOR_HUMAN', recovery_status(self.work)['human_input_request_id']))
        self.answer('The ANSWER_TEXT_42 is confidential')
        target2 = self.work.parent/'answered.zip'
        create_bundle(self.work, target2)
        with zipfile.ZipFile(target2) as archive:
            blob = b''.join(archive.read(n) for n in archive.namelist())
        self.assertNotIn(b'ANSWER_TEXT_42', blob)
        self.assertIn(b'human_input_received', blob)


if __name__ == '__main__':
    unittest.main()
