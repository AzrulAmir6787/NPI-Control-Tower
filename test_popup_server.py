"""Offline actual Python event-handler tests. No live database or auth calls."""
import ast
import copy
from pathlib import Path
import unittest

source = ast.parse(Path(__file__).with_name('app.py').read_text())
fn = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'render_drilldown')
event = next(n for n in fn.body if isinstance(n, ast.If) and 'component_event.get' in ast.unparse(n.test))
code = compile(ast.Module(body=[event], type_ignores=[]), 'actual_event_handler', 'exec')
class Rerun(Exception): pass
class ST:
    def __init__(self): self.session_state = {}
    def rerun(self): raise Rerun()
    def error(self, value): raise AssertionError(value)
class PopupTests(unittest.TestCase):
    def setUp(self):
        self.st = ST(); self.writes = []; self.rpc_calls = 0; self.mode = 'ok'
        self.latest = {}; self.previous = {}; self.ledger = {}
        def save(key, values, **kwargs):
            self.rpc_calls += 1
            if self.mode == 'denied': raise PermissionError('denied before RPC')
            if self.mode == 'before-timeout': raise TimeoutError('unknown network outcome')
            if key in self.latest: self.previous[key] = copy.deepcopy(self.latest[key])
            self.latest[key] = copy.deepcopy(values); self.writes.append(key)
            if self.mode == 'after-timeout': raise TimeoutError('committed, response lost')
            return values
        self.env = dict(st=self.st, popup_bridge_id='bridge-1', popup_requests=self.ledger,
                        safe_text=lambda v: '' if v is None else str(v), current_user_id='approved',
                        save_supabase_commitment_patch=save)
    def send(self, request, owner='owner', part='A', bridge='bridge-1', popup='popup-1'):
        self.env['component_event'] = {'type':'npi_commitment_save', 'payload':
            dict(request_id=request, popup_id=popup, bridge_id=bridge, part_key=part, owner=owner)}
        try: exec(code, self.env)
        except Rerun: pass
        return self.ledger.get(request)
    def test_four_consecutive_saves_and_old_replays(self):
        for i in range(4):
            result = self.send('r'+str(i), owner='v'+str(i))
            self.assertEqual(result['status'], 'saved')
            self.assertEqual(self.latest['A']['owner'], 'v'+str(i))
        for request in ['r0','r2','r3','r0']: self.send(request, owner='should never overwrite')
        self.assertEqual(self.rpc_calls, 4)
        self.assertEqual(self.latest['A']['owner'], 'v3')
        self.assertEqual(self.previous['A']['owner'], 'v2')
    def test_definite_access_failure_then_fresh_retry(self):
        self.mode = 'denied'
        self.assertEqual(self.send('denied')['status'], 'error')
        self.assertNotIn('_npi_commitment_success_message', self.st.session_state)
        self.mode = 'ok'
        self.send('denied')  # Same ID remains immutable: no write.
        self.assertEqual(self.rpc_calls, 1)
        self.assertEqual(self.send('fresh-retry')['status'], 'saved')
        self.assertEqual(self.writes, ['A'])
    def test_timeout_before_or_after_commit_never_retries_rpc(self):
        for mode in ['before-timeout','after-timeout']:
            with self.subTest(mode=mode):
                self.setUp(); self.mode = mode
                self.assertEqual(self.send('uncertain')['status'], 'unknown')
                self.mode = 'ok'
                for _ in range(3): self.send('uncertain')
                self.assertEqual(self.rpc_calls, 1)
                self.assertNotIn('_npi_commitment_success_message', self.st.session_state)
                self.assertEqual(len(self.writes), int(mode == 'after-timeout'))
    def test_empty_part_fails_without_rpc(self):
        self.assertEqual(self.send('invalid', part='')['status'], 'error')
        self.assertEqual(self.rpc_calls, 0)
    def test_other_bridge_ignored_and_other_part_unmodified(self):
        self.assertIsNone(self.send('wrong', bridge='other-tab'))
        self.assertEqual(self.rpc_calls, 0)
        self.send('b', part='B', owner='B1'); self.send('a', owner='A1'); self.send('a2', owner='A2')
        self.assertEqual(self.latest['B']['owner'], 'B1')
    def test_in_progress_ledger_prevents_reentrant_write(self):
        self.ledger['pending'] = dict(status='unknown', request_id='pending')
        self.send('pending')
        self.assertEqual(self.rpc_calls, 0)
if __name__ == '__main__': unittest.main(verbosity=2)
