"""On-demand mapping fallback must stay compact and fail closed."""
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))

from tools.mapping_resolver import MappingResolver
from tools.project import Project


class MappingResolverTests(unittest.TestCase):
    def _files(self, directory):
        root = Path(directory)
        mapping = {
            'groups': [{
                'hunk_mappings': [
                    {
                        'hunk': 0,
                        'mapping_type': 'merged_into',
                        'mappings': [{
                            'path': 'ActivityStarter.java',
                            'symbol': 'shouldAbortBackgroundActivityStart',
                            'evidence': 'target implementation',
                            'covers': ['visibility decision'],
                        }],
                        'required_bindings': ['visibleNotPinned || nonAppVisible'],
                        'reason': 'controller was folded into ActivityStarter',
                    },
                    {
                        'hunk': 9,
                        'mapping_type': 'create_method',
                        'mappings': [{
                            'path': 'ActivityRecord.java',
                            'symbol': 'deferStartingWindowRemoval',
                            'evidence': 'existing ActivityRecord lifecycle',
                        }],
                        'accessor_rewrites': [{
                            'source': 'mTransitionController.inTransition(this)',
                            'target': 'mAtmService.getTransitionController().inTransition(this)',
                        }],
                        'capability_resolutions': [{
                            'capability': 'keepAppWindowHideWhileLocked',
                            'resolution': 'INLINE_TRUE',
                            'evidence': 'flag fixed on in target',
                        }],
                    },
                    {
                        'hunk': 10,
                        'mapping_type': 'unresolved',
                        'reason': 'no target evidence',
                    },
                ]
            }]
        }
        mapping_path = root / 'mapping.json'
        mapping_path.write_text(json.dumps(mapping), encoding='utf-8')
        return str(mapping_path)

    def test_resolves_only_requested_hunks(self):
        with tempfile.TemporaryDirectory() as directory:
            resolver = MappingResolver(self._files(directory))
            result = resolver.resolve('0,9', source_symbol='callingUidHasVisibleNotPinnedActivity')

        self.assertEqual(result['status'], 'RESOLVED')
        self.assertEqual([row['hunk'] for row in result['hunks']], [0, 9])
        self.assertEqual(len(result['capability_resolutions']), 1)
        self.assertEqual(
            result['hunks'][1]['accessor_rewrites'][0]['source'],
            'mTransitionController.inTransition(this)',
        )
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 6000)

    def test_unresolved_mapping_is_evidence_required(self):
        with tempfile.TemporaryDirectory() as directory:
            resolver = MappingResolver(self._files(directory))
            result = resolver.resolve('10')

        self.assertEqual(result['status'], 'NEEDS_EVIDENCE')
        self.assertEqual(result['hunks'][0]['resolution'], 'unresolved')

    def test_search_fuse_cannot_record_no_change_before_mapping_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Project.__new__(Project)
            project.all_hunks_applied_succeeded = False
            project.round_succeeded = False
            project.context_mismatch_times = 0
            project.no_change_evidence = None
            project.search_fuse_active = True
            project.mapping_query_count = 0
            project.last_mapping_resolution = None
            project.mapping_resolver = MappingResolver(self._files(directory))

            result = project._validate('base', 'need not ported', 'target evidence')

        self.assertIn('MAPPING_FALLBACK_REQUIRED', result)
        self.assertFalse(project.round_succeeded)

    def test_identical_mapping_query_is_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Project.__new__(Project)
            project.mapping_resolver = MappingResolver(self._files(directory))
            project.mapping_query_count = 0
            project.last_mapping_resolution = None
            project.mapping_cache = {}

            first = project.resolve_mapping('base', '11', source_symbol='alwaysTruePredicate')
            second = project.resolve_mapping('base', '11', source_symbol='alwaysTruePredicate')

        self.assertEqual(project.mapping_query_count, 1)
        self.assertIn('cache_hit', second)
        self.assertNotIn('cache_hit', first)


if __name__ == '__main__':
    unittest.main()
