"""Offline regression of actual app read/event blocks; no Streamlit/Supabase needed."""
import ast
import copy
from pathlib import Path
import unittest

TREE = ast.parse(Path(__file__).with_name('app.py').read_text())
FN = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'render_drilldown')
READ = next(n for n in FN.body if isinstance(n, ast.If) and ast.unparse(n.test) == 'current_user_id')
EVENT = next(n for n in FN.body if isinstance(n, ast.If) and 'component_event.get' in ast.unparse(n.test))
FLASH = next(n for n in FN.body if isinstance(n, ast.Assign) and any(isinstance(v, ast.Name) and v.id == 'saved_message' for v in n.targets))
SHOW = FN.body[FN.body.index(FLASH) + 1]

def execute(nodes, env):
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual_app_blocks', 'exec'), env)

class Rerun(Exception):
    pass

class Frame:
    columns = ['Subpart Part Num']
    def __getitem__(self, key):
        return self
    def tolist(self):
        return ['A', 'B', 'C', 'D']

class FakeStreamlit:
    def __init__(self):
        self.session_state = {}
        self.messages = []
        self.errors = []
    def success(self, value): self.messages.append(value)
    def error(self, value): self.errors.append(value)
    def warning(self, value): raise AssertionError(value)
    def rerun(self): raise Rerun()

class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.st = FakeStreamlit()
        self.latest, self.previous, self.writes = {}, {}, []
        self.fail_key = None
        def read(keys, **kwargs):
            return copy.deepcopy({k: v for k, v in self.latest.items() if k in keys})
        def save(key, changes, **kwargs):
            if key == self.fail_key: raise RuntimeError('simulated RPC failure')
            if key in self.latest: self.previous[key] = copy.deepcopy(self.latest[key])
            self.latest[key] = {**self.latest.get(key, {}), **changes}
            self.writes.append(key)
        self.env = dict(st=self.st, current_user_id='approved-test-user',
            safe_text=lambda v: '' if v is None else str(v),
            part_col='Subpart Part Num', display_df=Frame(),
            get_supabase_commitments=read, save_supabase_commitment_patch=save)
    def read(self):
        self.env['commitment_lookup'] = {}
        execute([FLASH, SHOW, READ], self.env)
        return copy.deepcopy(self.env['commitment_lookup'])
    def event(self, request, records):
        self.env['component_event'] = {'type': 'npi_commitment_save', 'payload':
            {'request_id': request, 'records': records}}
        execute([EVENT], self.env)
    def save_and_refresh(self, request, records):
        self.read()  # The event run reads before saving, as in the actual app.
        with self.assertRaises(Rerun): self.event(request, records)
        snapshot = self.read()  # Explicit rerun reads Latest.
        before = len(self.writes)
        self.event(request, records)  # Component value persists across reruns.
        self.assertEqual(len(self.writes), before)
        self.assertEqual(snapshot, self.latest)
        return snapshot
    def test_ab_cd_edit_a_latest_and_previous(self):
        self.save_and_refresh('1', [{'part_key':'A','owner':'A1'}, {'part_key':'B','owner':'B1'}])
        self.assertEqual(self.previous, {})
        self.save_and_refresh('2', [{'part_key':'C','owner':'C1'}, {'part_key':'D','owner':'D1'}])
        self.assertEqual(self.previous, {})
        old = copy.deepcopy(self.latest)
        self.save_and_refresh('3', [{'part_key':'A','owner':'A2','remarks':'revised'}])
        self.assertEqual(self.previous, {'A':old['A']})
        for k in ['B','C','D']: self.assertEqual(self.latest[k], old[k])
        self.assertEqual(self.writes, ['A','B','C','D','A'])
        self.assertEqual(len(self.st.messages), 3)
        self.read()
        self.assertEqual(len(self.st.messages), 3)  # Flash appears once.
    def test_popup_single_record_payload(self):
        self.env['component_event'] = {'type':'npi_commitment_save', 'payload':
            {'request_id':'popup-1','part_key':'A','owner':'Popup'}}
        with self.assertRaises(Rerun): execute([EVENT], self.env)
        self.assertEqual(self.read()['A']['owner'], 'Popup')
        execute([EVENT], self.env)
        self.assertEqual(self.writes, ['A'])
    def test_failure_does_not_report_success_or_rerun(self):
        self.fail_key = 'B'
        self.event('failed', [{'part_key':'A','owner':'A1'}, {'part_key':'B','owner':'B1'}])
        self.assertEqual(self.writes, ['A'])
        self.assertEqual(len(self.st.errors), 1)
        self.assertNotIn('_npi_commitment_success_message', self.st.session_state)
    def test_empty_batch_does_not_rerun(self):
        self.event('empty', [])
        self.assertEqual(len(self.st.errors), 1)
        self.assertEqual(self.writes, [])
    def test_unrelated_event_ignored(self):
        self.env['component_event'] = None
        execute([EVENT], self.env)
        self.assertEqual(self.writes, [])

if __name__ == '__main__': unittest.main(verbosity=2)
