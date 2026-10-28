"""Real grammars and Git snapshots: scope, coordinate and navigation regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))
from tools.source_structure import (Source, SymbolIndex, hunk_coordinates,
                                    hunk_evidence, resolve_ref, snapshot, structure_relations,
                                    signature_changes)


class SourceStructureTests(unittest.TestCase):
    def test_full_file_overloads_distinguish_replacement_from_compatible_addition(self):
        before = Source('A.java', b'''class A {
 public void send(String x) {}
 private void send(String x, int uid) {}
}''')
        after = Source('A.java', b'''class A {
 public void send(String x) {}
 private void send(String x, boolean mode) {}
 private void send(String x, int uid, boolean mode) {}
}''')
        evidence = {'before': before.evidence([3], 2, 2), 'after': after.evidence([3, 4], 2, 3)}
        changes = signature_changes(before, after, evidence)
        self.assertEqual([(r['kind'], r['old_types'], r['new_types']) for r in changes],
                         [('replacement', ['String', 'int'], ['String', 'int', 'boolean']),
                          ('addition', None, ['String', 'boolean'])])
        self.assertFalse(any(r['old_types'] == ['String'] for r in changes))

    def test_boxed_java_type_is_not_a_compatible_primitive_overload(self):
        before = Source('A.java', b'class A { void f(Boolean mode) {} }')
        after = Source('A.java', b'class A { void f(boolean mode) {} }')
        evidence = {'before': before.evidence([1], 1, 1), 'after': after.evidence([1], 1, 1)}
        changes = signature_changes(before, after, evidence)
        self.assertEqual([(r['old_types'], r['new_types']) for r in changes], [(['Boolean'], ['boolean'])])

    def test_mirror_hash_requires_the_complete_scope_not_just_equal_hunk_text(self):
        a = Source('A.java', b'class A { void f() { change(); first(); } }')
        b = Source('B.java', b'class A { void f() { change(); second(); } }')
        c = Source('copy/A.java', a.data)
        digest = lambda s: s.evidence([1], 1, 1)['scopes'][0]['content_sha256']
        self.assertNotEqual(digest(a), digest(b))
        self.assertEqual(digest(a), digest(c))

    def test_argument_and_parameter_comments_do_not_count_as_values(self):
        source = Source('A.java', b'''class A {
 void send(int x, /* mode documentation */ boolean mode) {
   consume(x, /* mode */ true);
 }
}''')
        evidence = source.evidence([2, 3], 2, 2)
        self.assertEqual(evidence['scopes'][0]['parameter_types'], ['int', 'boolean'])
        self.assertEqual(evidence['calls'][0]['arguments'], ['x', 'true'])
        source = Source('a.c', b'void f(int x, /* context */ int y) { send(x, /* value */ y); }')
        self.assertEqual(len(source.evidence([1], 1, 1)['scopes'][0]['parameters']), 2)

    def test_changed_writes_are_distinct_from_shared_input_reads(self):
        source = Source('a.c', b'void f(int limit) {\n int k = limit;\n k = 1;\n assert(k < limit);\n}\n')
        evidence = source.evidence([2, 3, 4], 2, 3)
        self.assertEqual([(w['target'], w['value']) for w in evidence['writes']], [('k', 'limit'), ('k', '1')])

    def test_scopes_end_at_closing_brace_not_next_declaration(self):
        source = Source('A.java', b'class A {\n void f() {\n  first();\n }\n // between\n void g() {\n  second();\n }\n}\n')
        self.assertEqual([d['qualified_name'] for d in source.evidence([7], 6, 3)['scopes']], ['A.g'])
        self.assertEqual([d['qualified_name'] for d in source.evidence([5], 5, 1)['scopes']], ['A'])

    def test_c_struct_fields_and_late_multiline_function(self):
        source = Source('sqlite3.c', b'struct AggInfo {\n int iColumn;\n};\n' + b'\n'*1000 +
                        b'int\nlate(int x) {\n return send(x);\n}\n')
        self.assertEqual(source.status, 'ok')
        self.assertIn(('iColumn', 2), source.symbols())
        evidence = source.evidence([1006], 1004, 4)
        self.assertEqual([s['name'] for s in evidence['scopes']], ['late'])
        self.assertEqual(evidence['scopes'][0]['parameter_names'], ['x'])
        self.assertEqual(evidence['calls'][0]['arguments'], ['x'])

    def test_kotlin_signature_receiver_and_arguments(self):
        source = Source('A.kt', b'class A {\n fun run(flag: Boolean) {\n service.send(1, flag)\n }\n}\n')
        self.assertEqual(source.status, 'ok')
        evidence = source.evidence([3], 3, 1)
        self.assertEqual(evidence['scopes'][0]['parameter_names'], ['flag'])
        self.assertEqual(evidence['calls'][0]['callee'], 'service.send')
        self.assertEqual(evidence['calls'][0]['arguments'], ['1', 'flag'])
        self.assertFalse(evidence['scopes'][0]['signature_changed'])

    def test_lambda_fields_have_separate_owners(self):
        source = Source('A.java', b'class A {\n Rule first = x -> {\n  return check(x);\n };\n Rule second = x -> {\n  return check(x);\n };\n}\n')
        self.assertEqual(source.evidence([3], 3, 1)['scopes'][0]['qualified_name'], 'A.first')
        self.assertEqual(source.evidence([6], 6, 1)['scopes'][0]['qualified_name'], 'A.second')

    def test_cpp_rust_and_python_scopes(self):
        samples = [('a.cpp', b'namespace N { class A {\n void f(int x) {\n call(x);\n }\n}; }', 'N.A.f'),
                   ('a.rs', b'struct A; impl A {\n fn f(&self, x: i32) {\n call(x);\n }\n}', 'A.f'),
                   ('a.py', b'class A:\n def f(self, x):\n  return call(x)\n', 'A.f')]
        for path, data, expected in samples:
            with self.subTest(path=path):
                source = Source(path, data)
                self.assertEqual(source.status, 'ok')
                evidence = source.evidence([3], 3, 1)
                self.assertEqual(evidence['scopes'][0]['qualified_name'], expected)
                self.assertFalse(evidence['scopes'][0]['signature_changed'])

    def test_unsupported_and_parse_errors_are_explicit(self):
        self.assertEqual(Source('flags.aconfig', b'flag {}').status, 'unsupported')
        self.assertEqual(Source('A.java', b'class A { void broken( ').status, 'partial')
        with patch.dict(os.environ, {'SOURCE_STRUCTURE_CONFIG': ''}):
            source = Source('I.aidl', b'interface I { void run(in String value); }')
            self.assertEqual(source.status, 'unsupported')
            self.assertIn('AIDL', source.reason)

    def test_configured_aidl_uses_its_native_grammar(self):
        if not os.environ.get('SOURCE_STRUCTURE_CONFIG'):
            self.skipTest('Native AIDL grammar not configured')
        source = Source('I.aidl', b'interface I {\n oneway void run(in String value, boolean flag);\n}\n')
        self.assertEqual(source.status, 'ok')
        evidence = source.evidence([2], 2, 1)
        self.assertEqual(evidence['scopes'][0]['name'], 'run')
        self.assertEqual(evidence['scopes'][0]['parameter_names'], ['value', 'flag'])

    def test_insert_delete_and_rename_coordinates(self):
        h = {'file': 'Old.java', 'text': '--- a/Old.java\n+++ b/New.java\n@@ -3,0 +4,2 @@\n+x\n+y'}
        before, after = hunk_coordinates(h)
        self.assertEqual(before, ('Old.java', 3, 0, []))
        self.assertEqual(after, ('New.java', 4, 2, [4, 5]))
        h['text'] = '--- a/Old.java\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y'
        before, after = hunk_coordinates(h)
        self.assertEqual(before[3], [1, 2])
        self.assertEqual(after, ('/dev/null', 0, 0, []))

    def test_changed_lines_only_and_visible_context_truncation(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root)/'syntax.json'
            cfg.write_text(json.dumps({'context_lines': 2, 'max_context_lines': 2}))
            with patch.dict(os.environ, {'SOURCE_STRUCTURE_CONFIG': str(cfg)}):
                source = Source('A.java', b'class A {\n void f() {\n  changed();\n  unchanged();\n }\n}\n')
                evidence = source.evidence([3], 1, 6)
                self.assertEqual([c['callee'] for c in evidence['calls']], ['changed'])
                self.assertTrue(evidence['context_truncated'])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args]).decode().strip()

    def commit(self, text):
        (self.root/'A.java').write_text(text)
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def test_old_and_new_coordinates_map_to_their_own_revision(self):
        old = self.commit('class A {\n void first() {}\n void second() { oldCall(); }\n}\n')
        new = self.commit('class A {\n\n\n\n void first() {}\n void second() { newCall(); }\n}\n')
        evidence = hunk_evidence(self.root, old, new, {'file': 'A.java', 'text':
            '--- a/A.java\n+++ b/A.java\n@@ -3 +6 @@\n- void second() { oldCall(); }\n+ void second() { newCall(); }'})
        self.assertEqual(evidence['before']['scopes'][0]['qualified_name'], 'A.second')
        self.assertEqual(evidence['after']['scopes'][0]['qualified_name'], 'A.second')
        self.assertEqual(evidence['before']['calls'][0]['callee'], 'oldCall')
        self.assertEqual(evidence['after']['calls'][0]['callee'], 'newCall')

    def test_independent_cast_assignment_excludes_other_changes_on_the_line(self):
        old = self.commit('class A {\n void f(int x, int y) {\n  long size = x * y; use(size);\n }\n}\n')
        for tail, expected in [('use(size);', True), ('different(size);', False)]:
            with self.subTest(tail=tail):
                new = self.commit('class A {\n void f(int x, int y) {\n  long size = (long)x * y; '+tail+'\n }\n}\n')
                evidence = hunk_evidence(self.root, old, new, {'file': 'A.java', 'text':
                    '--- a/A.java\n+++ b/A.java\n@@ -3 +3 @@\n-  long size = x * y; use(size);\n+  long size = (long)x * y; '+tail})
                self.assertEqual('cast_only_assignment' in evidence, expected)

    def test_cast_only_assignment_covers_multiple_control_paths_in_one_hunk(self):
        old = self.commit('class A {\n long f(int x, int y) {\n  long size;\n  if (x > 0) {\n   size = x * y;\n  } else {\n   size = x * y;\n  }\n  return size;\n }\n}\n')
        new = self.commit((self.root/'A.java').read_text().replace('size = x * y;', 'size = (long)x * y;'))
        diff = self.git('diff', old, new, '--', 'A.java')
        evidence = hunk_evidence(self.root, old, new, {'file': 'A.java', 'text': diff})
        self.assertEqual([r['target'] for r in evidence['cast_only_assignment']], ['size', 'size'])

    def test_symbol_lookup_preserves_dirty_worktree_and_refreshes_moving_refs(self):
        old = self.commit('class A { void before() {} }\n')
        index = SymbolIndex(self.root)
        (self.root/'A.java').write_text('uncommitted user edit\n')
        self.assertEqual(index.locate('HEAD', 'before'), [('A.java', 1)])
        self.assertEqual((self.root/'A.java').read_text(), 'uncommitted user edit\n')
        new = self.commit('class A { void after() {} }\n')
        self.assertIsNone(index.locate('HEAD', 'before'))
        self.assertEqual(index.locate(old, 'before'), [('A.java', 1)])
        self.assertIn('after', index.prepare('HEAD'))
        self.assertNotIn('before', index.prepare(new))
        self.assertFalse((self.root/'tags').exists())

    def test_cache_distinguishes_repositories_and_missing_files(self):
        sha = self.commit('class A {}\n')
        self.assertEqual(snapshot(self.root, sha, 'Missing.java').status, 'missing')
        with self.assertRaises(subprocess.CalledProcessError):
            resolve_ref(self.root, 'not-a-ref')

    def test_new_file_and_mismatched_revision(self):
        old = self.commit('class A {}\n')
        (self.root/'B.java').write_text('class B {}\n')
        new = self.commit('class A { }\n')
        evidence = hunk_evidence(self.root, old, new, {'file': '(new file)', 'text':
            '--- /dev/null\n+++ b/B.java\n@@ -0,0 +1 @@\n+class B {}'})
        self.assertEqual(evidence['before']['status'], 'missing')
        self.assertEqual(evidence['after']['scopes'][0]['name'], 'B')
        with self.assertRaisesRegex(ValueError, 'Patch/source mismatch'):
            hunk_evidence(self.root, old, new, {'file': 'A.java', 'text':
                '--- a/A.java\n+++ b/A.java\n@@ -1 +1 @@\n-wrong source\n+class A { }'})

    def test_parameter_prerequisite_is_scoped_and_not_a_merge_edge(self):
        old = self.commit('class A {\n void f() {\n  oldCall();\n }\n void g(boolean flag) {\n  oldCall();\n }\n}\n')
        new = self.commit('class A {\n void f(boolean flag) {\n  if (flag) oldCall();\n }\n void g(boolean flag) {\n  if (flag) oldCall();\n }\n}\n')
        diffs = [
            '@@ -2 +2 @@\n- void f() {\n+ void f(boolean flag) {',
            '@@ -3 +3 @@\n-  oldCall();\n+  if (flag) oldCall();',
            '@@ -6 +6 @@\n-  oldCall();\n+  if (flag) oldCall();',
        ]
        records = [{'hunk': i, 'source_structure': hunk_evidence(self.root, old, new,
                    {'file': 'A.java', 'text': '--- a/A.java\n+++ b/A.java\n'+diff})}
                   for i, diff in enumerate(diffs)]
        edges = structure_relations(records)
        self.assertEqual([(e['provider'], e['consumer']) for e in edges
                          if e['kind'] == 'parameter_prerequisite'], [(0, 1)])


if __name__ == '__main__':
    unittest.main()
