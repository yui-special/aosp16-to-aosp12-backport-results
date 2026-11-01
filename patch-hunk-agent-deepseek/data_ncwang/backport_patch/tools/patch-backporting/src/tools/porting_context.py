"""Deterministic session context: committed changes and read progress."""
import json
import re

from tools.utils import split_patch_indexed


class ChangeLedger:
    def __init__(self, limit=6000):
        self.limit = limit
        self.entries = []

    def record(self, group, members, base, ref, patch):
        if not patch.strip():
            return
        _, hunks = split_patch_indexed(patch, True)
        self.entries.append(dict(group=group, members=members, base=base, ref=ref, hunks=hunks))

    def render(self, files=(), symbols=(), cumulative=True):
        if not self.entries:
            return ''
        files = set(files)
        symbols = {s.replace('::', '.').rsplit('.', 1)[-1] for s in symbols}
        index, candidates = [], []
        for order, entry in enumerate(self.entries):
            for number, hunk in enumerate(entry['hunks']):
                header = next((line for line in hunk['text'].splitlines() if line.startswith('@@')), '')
                key = '%s:%s' % (entry['group'], number)
                index.append(dict(id=key, group=entry['group'], members=entry['members'],
                                  base=entry['base'], ref=entry['ref'], path=hunk['file'], range=header))
                ids = set(re.findall(r'\b[A-Za-z_]\w*\b', hunk['text']))
                score = 2 * len(ids & symbols) + int(hunk['file'] in files)
                candidates.append((score, order, key, hunk['text']))
        remaining, included, details = self.limit, set(), []
        for score, _, key, patch in sorted(candidates, key=lambda row: (-row[0], -row[1], row[2])):
            # Whole hunks only: never cut a method or a diff line mid-way.
            if score and len(patch) <= remaining:
                included.add(key)
                details.append('change %s\n%s' % (key, patch))
                remaining -= len(patch)
        for row in index:
            row['detail'] = 'included' if row['id'] in included else 'omitted; inspect path/range at ref'
        note = ('These are committed group diffs; the current target ref is authoritative.' if cumulative
                else 'These are the latest group candidates, not applied to the target ref.')
        return ('\n[PREVIOUS CHANGES]\n' + note + '\n' + json.dumps(index, ensure_ascii=False)
                + '\n' + '\n'.join(details) + '\n')


class QueryProgress:
    """Track newly returned source facts; this is not a semantic coverage score."""
    def __init__(self, stale_limit=4):
        self.stale_limit = stale_limit
        self.seen = set()
        self.stale = 0
        self.queries = []
        self.validations = 0

    def observe(self, tool, query, facts, output):
        facts = {json.dumps(fact, sort_keys=True, ensure_ascii=False, default=str) for fact in facts}
        new = facts - self.seen
        self.seen.update(facts)
        self.stale = 0 if new else self.stale + 1
        self.queries.append(dict(tool=tool, query=query, new_facts=len(new), stale_reads=self.stale))
        if self.stale >= self.stale_limit:
            return ('[QUERY STALLED] This read adds no new source facts. Submit a candidate with validate '
                    'if the evidence is sufficient. Otherwise name the missing fact and make a targeted '
                    'query, or finish with that explicit blocker. Do not infer that a hunk is unnecessary. '
                    'The repeated result is already in the session.\n')
        return output

    def validated(self):
        self.validations += 1
        self.stale = 0

    def report(self):
        return dict(validations=self.validations, stale_reads=self.stale, queries=self.queries)


def execution_result(result, succeeded=False, error=None):
    """Record the executor's actual result, not an inferred iteration limit."""
    output = str((result or {}).get('output', ''))
    if error is not None:
        reason = 'exception'
        output = '%s: %s' % (type(error).__name__, error)
    elif succeeded:
        reason = 'candidate_recorded'
    elif output.strip() in {'Agent stopped due to iteration limit or time limit.',
                            'Agent stopped due to max iterations.'}:
        reason = 'executor_limit'
    else:
        reason = 'returned_without_validated_candidate'
    return dict(reason=reason, output=output)
