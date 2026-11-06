import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                      'data_ncwang/backport_patch/tools/patch-backporting/src'))
from tools.porting_context import ApiContext, ChangeLedger, QueryProgress, execution_result


def diff(path, old, new):
    return ('diff --git a/{0} b/{0}\n--- a/{0}\n+++ b/{0}\n'
            '@@ -1 +1 @@\n-{1}\n+{2}\n').format(path, old, new)


class PortingContextTests(unittest.TestCase):
    def test_unlimited_queries_do_not_stall_or_truncate_source(self):
        progress = QueryProgress(stale_limit=None)
        for _ in range(40):
            self.assertEqual(progress.observe('read', {}, [('sha', 1)], 'same'), 'same')
        text, facts = progress.source_excerpt('sha', 'A.java', 1, ['x' * 15000], max_chars=None)
        self.assertIn('x' * 15000, text)
        self.assertEqual(len(facts), 1)

    def test_seeded_context_deduplicates_only_delivered_lines_at_the_same_version(self):
        progress = QueryProgress()
        progress.seed_feedback('CANDIDATE_FAILURE_CONTEXT: []')
        progress.seed_feedback('CANDIDATE_FAILURE_CONTEXT: {"frames":[null,42]}')
        context = dict(candidate_tree='candidate', frames=[dict(path='A.java', start_line=10,
            end_line=12, quote='10: ten\n11: eleven\n12: cut', quote_complete=False)])
        progress.seed_feedback('CANDIDATE_FAILURE_CONTEXT: ' + json.dumps(context))
        text, facts = progress.source_excerpt('candidate', 'A.java', 10, ['ten', 'eleven', 'twelve'])
        self.assertNotIn('10: ten', text)
        self.assertIn('12: twelve', text)
        self.assertEqual(facts, [('candidate', 'A.java', 12)])
        old, facts = progress.source_excerpt('base', 'A.java', 10, ['old ten'])
        self.assertIn('10: old ten', old)
        self.assertEqual(facts, [('base', 'A.java', 10)])
        actual, _ = progress.source_excerpt('candidate', 'A.java', 10, ['different real source'])
        self.assertIn('different real source', actual)
        fresh, _ = QueryProgress().source_excerpt('candidate', 'A.java', 10, ['ten'])
        self.assertIn('10: ten', fresh)

    def test_source_limit_never_marks_hidden_or_partial_lines_as_delivered(self):
        progress = QueryProgress()
        text, facts = progress.source_excerpt('sha', 'A.java', 1, ['small', 'x'*5000], 150)
        self.assertEqual(facts, [('sha', 'A.java', 1)])
        self.assertIn('TRUNCATED before line 2', text)
        self.assertEqual(progress.source_seen, set())

    def test_known_target_context_seeds_only_delivered_source_at_its_exact_revision(self):
        progress = QueryProgress()
        context = dict(ref='commit', queries=[dict(symbol='A.hidden', definitions=[dict(
            path='A.java', start=20, end=30)])], sources=[dict(path='A.java', start_line=4, end_line=6,
            quote='4: setup();\n5: check();\n6: partial', quote_complete=False)])
        progress.seed_feedback('[KNOWN TARGET FACTS]\n' + json.dumps(context))
        text, facts = progress.source_excerpt('commit', 'A.java', 4, ['setup();', 'check();', 'complete();'])
        self.assertNotIn('4: setup();', text)
        self.assertEqual(facts, [('commit', 'A.java', 6)])
        for ref, path, line, source in [('other-commit', 'A.java', 4, 'setup();'),
                                       ('commit', 'Other.java', 4, 'setup();'),
                                       ('commit', 'A.java', 20, 'hidden();'),
                                       ('commit', 'A.java', 4, 'different();')]:
            self.assertEqual(progress.source_excerpt(ref, path, line, [source])[1], [(ref, path, line)])
        context['sources'] = [dict(path='A.java', start_line=6, end_line=6,
                                   quote='6: complete();', quote_complete=True)]
        progress.seed_feedback('[KNOWN TARGET FACTS]\n' + json.dumps(context))
        text, facts = progress.source_excerpt('commit', 'A.java', 4, ['setup();', 'check();', 'complete();'])
        self.assertEqual(facts, [])
        self.assertIn('NO_NEW_SOURCE', text)
        # A new group has a new message history and must not inherit hidden reads.
        self.assertTrue(QueryProgress().source_excerpt('commit', 'A.java', 4, ['setup();'])[1])

    def test_fully_repeated_range_is_explicit_but_new_lines_remain_available_without_caps(self):
        progress = QueryProgress(stale_limit=None)
        _, facts = progress.source_excerpt('candidate', 'A.java', 1, ['one', 'two'])
        progress.source_seen.update(facts)
        repeated, facts = progress.source_excerpt('candidate', 'A.java', 1, ['one', 'two'])
        self.assertIn('NO_NEW_SOURCE', repeated)
        self.assertEqual(facts, [])
        expanded, facts = progress.source_excerpt('candidate', 'A.java', 2, ['two', 'three'])
        self.assertNotIn('NO_NEW_SOURCE', expanded)
        self.assertEqual(facts, [('candidate', 'A.java', 3)])

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


class ApiContextPaginationTests(unittest.TestCase):
    def test_small_inline_inventory_is_complete_only_when_its_prompt_is_delivered(self):
        context = self.context(2, inline_chars=6000)
        prompt = context.prompt()
        self.assertIn('INLINE API RECORDS', prompt)
        self.assertEqual(context.read_report()['missing'], ['reset'])
        context.record_inline_read(context.inline_payload[:-1])
        self.assertEqual(context.read_report()['missing'], ['reset'])
        context.record_inline_read(prompt)
        self.assertEqual(context.read_report()['missing'], [])
        other = self.context(2, inline_chars=6000)
        other.inventory['changed_tree'] = True
        # A new context/ref is represented by a different inventory digest.
        fresh = ApiContext(other.inventory, 'group.json', inline_chars=6000)
        fresh.prompt()
        fresh.record_inline_read(prompt)
        self.assertEqual(fresh.read_report()['missing'], ['reset'])

    def test_large_or_disabled_inline_inventory_keeps_all_pagination_requirements(self):
        for budget in (0, 100):
            context = self.context(21, inline_chars=budget)
            prompt = context.prompt()
            self.assertNotIn('[INLINE API RECORDS', prompt)
            context.record_inline_read(prompt)
            self.assertEqual(context.read_report()['pending']['reset']['references']['count'], 21)

    def context(self, count=21, **kwargs):
        return ApiContext({'apis': [dict(symbol='reset', ref='fixed-tree', definitions=[],
            references=[dict(path='A.java' if i % 2 else 'B.java', line=i+1, end_line=i+1,
                             expression='reset(1)', scope='A.run', argument_count=1)
                        for i in range(count)])]}, 'group.json', **kwargs)

    def test_first_page_cannot_satisfy_a_twenty_one_callsite_inventory(self):
        context = self.context()
        first = context.query('reset')
        self.assertEqual(first['pages']['references']['next_offset'], 10)
        self.assertEqual(context.read_report()['missing'], ['reset'])
        # Neither repeated pages nor jumping to the end closes the unseen middle.
        context.query('reset')
        context.query('reset', record_type='references', offset=20)
        self.assertEqual(context.read_report()['pending']['reset']['references']['count'], 10)
        context.query('reset', record_type='references', offset=10)
        self.assertEqual(context.read_report()['missing'], [])

    def test_filtered_pages_count_original_records_without_double_counting(self):
        context = self.context(6, page_limit=2)
        context.query('reset', path='A.java', limit=2)
        context.query('reset', path='A.java', limit=2)
        context.query('reset', path='A.java', offset=2, limit=2)
        self.assertEqual(context.read_report()['pending']['reset']['references']['count'], 3)
        context.query('reset', path='B.java', offset=2, limit=2)
        context.query('reset', path='B.java', limit=2)
        self.assertEqual(context.read_report()['missing'], [])

    def test_oversized_record_needs_complete_source_at_the_right_ref(self):
        context = self.context(2, max_chars=2000)
        row = context.apis['reset']['references'][0]
        row.update(expression='x'*5000, line=10, end_line=20)
        first = context.query('reset')
        self.assertEqual(first['pages']['references']['next_offset'], 1)
        self.assertEqual(first['pages']['references']['deferred'], 1)
        context.query('reset', record_type='references', offset=1)
        context.record_source_read('another-tree', 'B.java', 10, 20)
        self.assertEqual(context.read_report()['missing'], ['reset'])
        context.record_source_read('fixed-tree', 'B.java', 10, 14)
        self.assertEqual(context.read_report()['missing'], ['reset'])
        context.record_source_read('fixed-tree', 'B.java', 16, 20)
        self.assertEqual(context.read_report()['missing'], ['reset'])
        context.record_source_read('fixed-tree', 'B.java', 15, 15)
        self.assertEqual(context.read_report()['missing'], [])

    def test_bound_includes_metadata_and_both_record_types(self):
        context = self.context(5, max_chars=2000)
        context.apis['reset']['definitions'] = [dict(path='A.java', start=1, end=2,
                                                     declaration='x'*900)]
        for row in context.apis['reset']['references']:
            row['expression'] = 'x'*500
        response = context.query('reset')
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=False)), 2000)
        self.assertEqual(context.read_report()['missing'], ['reset'])


if __name__ == '__main__':
    unittest.main()
