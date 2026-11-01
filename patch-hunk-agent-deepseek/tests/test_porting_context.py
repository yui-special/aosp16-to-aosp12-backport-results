import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                      'data_ncwang/backport_patch/tools/patch-backporting/src'))
from tools.porting_context import ChangeLedger, QueryProgress, execution_result


def diff(path, old, new):
    return ('diff --git a/{0} b/{0}\n--- a/{0}\n+++ b/{0}\n'
            '@@ -1 +1 @@\n-{1}\n+{2}\n').format(path, old, new)


class PortingContextTests(unittest.TestCase):
    def test_ledger_selects_later_relevant_hunks_without_cutting_them(self):
        large = diff('Unrelated.java', 'x' * 7000, 'y' * 7000)
        relevant = diff('Api.java', 'void reset(int user) {}',
                        'void reset(int user, boolean restore) {}')
        ledger = ChangeLedger(limit=700)
        ledger.record(0, [0, 1], 'base', 'commit', large + relevant)
        result = ledger.render(symbols=['Api.reset'])
        self.assertIn(relevant.split('\n', 1)[1].strip(), result)
        self.assertNotIn('y' * 100, result)
        self.assertIn('Unrelated.java', result)
        self.assertIn('omitted; inspect path/range at ref', result)
        self.assertIn('"ref": "commit"', result)

    def test_ledger_budget_is_whole_hunks_and_zero_retains_location_index(self):
        patch = diff('A.java', 'old()', 'newCall()')
        ledger = ChangeLedger(limit=0)
        ledger.record(3, [5], 'before', 'after', patch)
        result = ledger.render(files=['A.java'])
        self.assertNotIn('+newCall()', result)
        self.assertIn('A.java', result)
        self.assertIn('@@ -1 +1 @@', result)
        self.assertIn('"members": [5]', result)

    def test_query_progress_suppresses_only_repeated_results_and_resets_after_validation(self):
        progress = QueryProgress(stale_limit=2)
        progress.observe('read', {}, [('sha', 'A.java', 1)], 'code')
        progress.observe('read', {}, [('sha', 'A.java', 1)], 'code')
        self.assertIn('QUERY STALLED', progress.observe('read', {}, [('sha', 'A.java', 1)], 'code'))
        self.assertEqual(progress.observe('read', {}, [('sha', 'A.java', 2)], 'more code'), 'more code')
        progress.validated()
        self.assertEqual(progress.report()['validations'], 1)
        self.assertEqual(progress.report()['stale_reads'], 0)
        self.assertEqual(progress.report()['queries'][-1]['new_facts'], 1)

    def test_parse_gaps_are_returned_before_duplicate_suppression(self):
        progress = QueryProgress(stale_limit=1)
        fact = dict(ref='sha', diagnostics=[dict(path='I.aidl', status='unsupported')])
        self.assertEqual(progress.observe('locate', {}, [fact], json.dumps(fact)), json.dumps(fact))
        self.assertIn('QUERY STALLED', progress.observe('locate', {}, [fact], json.dumps(fact)))

    def test_exit_reasons_distinguish_normal_return_limit_and_exception(self):
        self.assertEqual(execution_result({'output': 'Missing target receiver type'})['reason'],
                         'returned_without_validated_candidate')
        self.assertEqual(execution_result({'output': 'Agent stopped due to iteration limit or time limit.'})['reason'],
                         'executor_limit')
        self.assertEqual(execution_result(None, error=ValueError('bad response'))['reason'], 'exception')
        self.assertEqual(execution_result({'output': 'done'}, succeeded=True)['reason'], 'candidate_recorded')


if __name__ == '__main__':
    unittest.main()
