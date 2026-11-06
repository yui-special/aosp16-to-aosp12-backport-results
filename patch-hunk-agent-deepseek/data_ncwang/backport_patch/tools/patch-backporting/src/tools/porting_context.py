"""Deterministic session context: committed changes and read progress."""
import hashlib
import json
import re

from tools.utils import split_patch_indexed
from tools.source_structure import normalize_symbol, resolve_source_ref


def target_context(index, repo, ref, group, limit=6000):
    """Reuse exact-revision query facts and read mapped methods via the existing index."""
    revision = resolve_source_ref(index.repo, ref)
    wanted = set(re.findall(r'\b[A-Za-z_]\w*\b', group.get('text', '') + ' ' +
                           ' '.join(group.get('target_symbols', []))))
    for location in group.get('target_locations', []):
        index.inspect(revision, location['symbol'], path=location['path'])
        wanted.add(normalize_symbol(location['symbol']).rsplit('.', 1)[-1])
    facts, sources, seen, seen_source = [], [], set(), set()
    remaining = limit
    # Recent queries include mapped target methods; older queries provide reuse
    # across groups only when they refer to this exact immutable revision.
    for (sha, symbol, path, _), result in reversed(list(index.inspections.items())):
        if sha != revision or normalize_symbol(symbol).rsplit('.', 1)[-1] not in wanted:
            continue
        row = dict(symbol=symbol, searched_path=path, scope_status=result.get('scope_status'),
                   query_status=result.get('query_status'), diagnostics=result.get('diagnostics', []),
                   definitions=[{k: d[k] for k in ('path', 'qualified_name', 'kind', 'start', 'end', 'signature')
                                 if k in d} for d in result.get('definitions', [])],
                   call_candidates=len(result.get('references', [])))
        encoded = json.dumps(row, ensure_ascii=False)
        if encoded in seen or len(encoded) > remaining:
            continue
        seen.add(encoded)
        remaining -= len(encoded)
        facts.append(row)
        for definition in row['definitions']:
            if definition['kind'] != 'function':
                continue
            key = (definition['path'], definition['start'], definition['end'])
            if key in seen_source:
                continue
            seen_source.add(key)
            lines = repo.git.show('%s:%s' % (revision, key[0])).splitlines()
            quote = '\n'.join('%d: %s' % (n, lines[n - 1]) for n in range(key[1], key[2] + 1))
            if len(quote) <= remaining:
                sources.append(dict(path=key[0], start_line=key[1], end_line=key[2], quote=quote,
                                    quote_complete=True))
                remaining -= len(quote)
    if not facts:
        return ''
    return ('\n[KNOWN TARGET FACTS]\n' + json.dumps(dict(ref=revision, queries=facts, sources=sources),
            ensure_ascii=False) + '\nReuse these exact-revision query results. Methods without source excerpts '
            'remain readable by their paths/ranges. Call counts are candidates, not a proven API closure. '
            'A scoped miss or parse gap does not prove absence or remove a behavioral/test obligation.\n')


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
        self.source_seen = set()
        self.seeded_source = {}

    def seed_feedback(self, feedback):
        """Register only complete numbered lines actually delivered in this session."""
        for marker, ref_key, rows_key in (
                ('CANDIDATE_FAILURE_CONTEXT:', 'candidate_tree', 'frames'),
                ('CANDIDATE_DIAGNOSTIC_CONTEXT:', 'candidate_tree', 'frames'),
                ('[KNOWN TARGET FACTS]', 'ref', 'sources')):
            offset = 0
            while (offset := feedback.find(marker, offset)) != -1:
                offset += len(marker)
                try:
                    context, _ = json.JSONDecoder().raw_decode(feedback[offset:].lstrip())
                except ValueError:
                    continue
                if not isinstance(context, dict) or not isinstance(context.get(rows_key, []), list):
                    continue
                ref = context.get(ref_key)
                for frame in context.get(rows_key, []):
                    if not isinstance(frame, dict) or not isinstance(frame.get('quote', ''), str):
                        continue
                    rows = frame.get('quote', '').splitlines()
                    if not frame.get('quote_complete', frame.get('scope_complete', False)):
                        rows = rows[:-1]
                    for row in rows:
                        number, separator, text = row.partition(': ')
                        if (isinstance(ref, str) and isinstance(frame.get('path'), str)
                                and separator and number.isdigit()):
                            key = (ref, frame['path'], int(number))
                            self.source_seen.add(key)
                            self.seeded_source[key] = text

    def source_excerpt(self, ref, path, start, lines, max_chars=12000):
        """Return new complete lines; callers record them only after successful delivery."""
        output = '%s:%s lines %d-%d\n' % (ref, path, start, start + len(lines) - 1)
        facts, skipped = [], 0
        for number, line in enumerate(lines, start):
            fact = (ref, path, number)
            if fact in self.source_seen and self.seeded_source.get(fact, line) == line:
                skipped += 1
                continue
            row = '%d: %s\n' % (number, line)
            if max_chars is not None and len(output) + len(row) > max_chars:
                output += '[SOURCE TRUNCATED before line %d; request a narrower range]\n' % number
                break
            output += row
            facts.append(fact)
        if skipped:
            output += '[%d source lines already provided at this same ref/path were omitted]\n' % skipped
            if not facts and skipped == len(lines):
                output += ('[NO_NEW_SOURCE: the entire requested range is already in this session. '
                           'Reuse it; query a different missing fact only if needed.]\n')
        return output, facts

    def observe(self, tool, query, facts, output):
        facts = {json.dumps(fact, sort_keys=True, ensure_ascii=False, default=str) for fact in facts}
        new = facts - self.seen
        self.seen.update(facts)
        self.stale = 0 if new else self.stale + 1
        self.queries.append(dict(tool=tool, query=query, new_facts=len(new), stale_reads=self.stale))
        if self.stale_limit is not None and self.stale >= self.stale_limit:
            return ('[QUERY STALLED] This read adds no new source facts. Use the available submission tool '
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


class ApiContext:
    """A bounded, read-only view of one group's API inventory."""

    def __init__(self, inventory, artifact_name, page_limit=10, max_chars=24000, inline_chars=6000):
        self.inventory = inventory
        self.artifact_name = artifact_name
        self.page_limit = max(1, min(20, page_limit))
        self.max_chars = max(2000, max_chars)
        self.apis = {row['symbol']: row for row in inventory.get('apis', [])}
        self.digest = hashlib.sha256(json.dumps(
            inventory, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        self.reads = {}
        self.deferred_reads = {}
        self.source_reads = {}
        self.inline_chars = max(0, inline_chars)
        self.inline_payload = ''
        self.inline_symbols = []

    def manifest(self):
        return dict(
            artifact=self.artifact_name,
            sha256=self.digest,
            page_limit=self.page_limit,
            max_chars=self.max_chars,
            symbols=[dict(symbol=symbol,
                          definitions=len(self.apis[symbol].get('definitions', [])),
                          references=len(self.apis[symbol].get('references', [])))
                     for symbol in sorted(self.apis)],
        )

    def read_report(self):
        completed, pending = [], {}
        for symbol in sorted(self.apis):
            missing = {kind: sorted(set(range(len(self.apis[symbol].get(kind, []))))
                                   - self.reads.get((symbol, kind), set()))
                       for kind in ('definitions', 'references')}
            if not any(missing.values()):
                completed.append(symbol)
            else:
                pending[symbol] = {kind: dict(count=len(indices), next_record=indices[0])
                                   for kind, indices in missing.items() if indices}
        required = sorted(self.apis)
        return dict(required=required, completed=completed,
                    missing=[s for s in required if s not in completed], pending=pending)

    def _rows(self, api, kind, path, scope, argument_count):
        rows = []
        for index, row in enumerate(api.get(kind, [])):
            if path and row.get('path') != path:
                continue
            if scope and row.get('scope', row.get('qualified_name')) != scope:
                continue
            if argument_count is not None:
                count = (len(row.get('parameters', [])) if kind == 'definitions'
                         else row.get('argument_count'))
                if count != argument_count:
                    continue
            rows.append((index, row))
        return rows

    def query(self, symbol, record_type='all', path='', scope='',
              argument_count=None, offset=0, limit=10, *, record_read=True):
        if symbol not in self.apis:
            raise ValueError('symbol is outside this group API context: %s' % symbol)
        if record_type not in {'all', 'definitions', 'references'}:
            raise ValueError('record_type must be all, definitions or references')
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be an integer >= 0')
        if type(limit) is not int or not 1 <= limit <= self.page_limit:
            raise ValueError('limit must be between 1 and %d' % self.page_limit)
        if argument_count is not None and (type(argument_count) is not int or argument_count < 0):
            raise ValueError('argument_count must be an integer >= 0')
        api = self.apis[symbol]
        kinds = ('definitions', 'references') if record_type == 'all' else (record_type,)
        result = dict(artifact=self.artifact_name, sha256=self.digest, symbol=symbol,
                      filters=dict(path=path, scope=scope, argument_count=argument_count),
                      pages={}, oversized=[],
                      note='Name/arity candidates only; empty results do not prove absence.')
        filtered = {kind: self._rows(api, kind, path, scope, argument_count) for kind in kinds}

        def page_info(kind, indices, deferred=0):
            total = len(filtered[kind])
            following = offset + len(indices) + deferred
            result['pages'][kind] = dict(
                total=total, offset=offset, returned=len(indices), record_indices=list(indices),
                deferred=deferred, next_offset=following if following < total else None,
                truncated=following < total or bool(deferred))

        for kind in kinds:
            result[kind] = []
            page_info(kind, [])
        for kind in kinds:
            rows, indices = filtered[kind], []
            for index, row in rows[offset:offset + limit]:
                result[kind].append(row)
                indices.append(index)
                page_info(kind, indices)
                if len(json.dumps(result, ensure_ascii=False)) > self.max_chars:
                    result[kind].pop()
                    indices.pop()
                    page_info(kind, indices)
                    break
            if not indices and offset < len(rows):
                index, row = rows[offset]
                result['oversized'].append(dict(
                    record_type=kind, record_index=index, ref=api.get('ref'), path=row.get('path'),
                    line=row.get('line', row.get('start')),
                    end_line=row.get('end_line', row.get('end')),
                    action='Read this exact range with viewcode, or query this record_type alone. '
                           'This pointer does not count as a completed read.'))
                page_info(kind, indices, deferred=1)
                if len(json.dumps(result, ensure_ascii=False)) > self.max_chars:
                    result['oversized'].pop()
                    page_info(kind, indices)
        if len(json.dumps(result, ensure_ascii=False)) > self.max_chars:
            raise ValueError('Query metadata exceeds context_query_chars; shorten filters')
        if record_read:
            self.record_query_read(result)
        return result

    def record_query_read(self, result):
        """Called only after the complete response is actually delivered to the model."""
        symbol = result['symbol']
        for kind, page in result['pages'].items():
            self.reads.setdefault((symbol, kind), set()).update(page['record_indices'])
        for row in result['oversized']:
            self.deferred_reads[(symbol, row['record_type'], row['record_index'])] = row
        self._complete_source_reads()

    def record_source_read(self, ref, path, start, end):
        ranges = self.source_reads.setdefault((ref, path), [])
        merged = []
        for first, last in sorted(ranges + [(start, end)]):
            if merged and first <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
            else:
                merged.append((first, last))
        self.source_reads[(ref, path)] = merged
        self._complete_source_reads()

    def _complete_source_reads(self):
        for (symbol, kind, index), row in self.deferred_reads.items():
            start, end = row['line'], row['end_line']
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                continue
            if any(first <= start and last >= end
                   for first, last in self.source_reads.get((row['ref'], row['path']), [])):
                self.reads.setdefault((symbol, kind), set()).add(index)

    def record_inline_read(self, delivered_prompt):
        """A rendered prompt alone is not delivery; the executor calls this at invoke."""
        if not self.inline_payload or self.inline_payload not in delivered_prompt:
            return
        for symbol in self.inline_symbols:
            for kind in ('definitions', 'references'):
                self.reads.setdefault((symbol, kind), set()).update(range(len(self.apis[symbol].get(kind, []))))

    def prompt(self):
        manifest = self.manifest()
        signatures = [dict(hunk=row.get('hunk'), name=row.get('name'),
                           kind=row.get('kind'), old_types=row.get('old_types'),
                           new_types=row.get('new_types'), path=row.get('path'))
                      for row in self.inventory.get('upstream_signature_changes', [])]
        inline = dict(artifact=self.artifact_name, sha256=self.digest, apis=[])
        self.inline_payload, self.inline_symbols = '', []
        for symbol in sorted(self.apis):
            api = self.apis[symbol]
            item = {k: api.get(k, [] if k in {'definitions', 'references'} else '')
                    for k in ('symbol', 'ref', 'definitions', 'references')}
            inline['apis'].append(item)
            encoded = json.dumps(inline, ensure_ascii=False)
            if len(encoded) <= self.inline_chars:
                self.inline_payload = '[INLINE API RECORDS — COMPLETE SYMBOLS]\n' + encoded
                self.inline_symbols.append(symbol)
            else:
                inline['apis'].pop()
        required = '、'.join(s for s in sorted(self.apis) if s not in self.inline_symbols) or '(none)'
        return (
            '\n[TARGET API CONTEXT]\n'
            'Complete small symbol inventories are inlined below. Do not query them again '
            'just to complete a read requirement. Other symbols remain available through '
            'query_api_context for this current-group artifact; results are paginated.\n'
            + json.dumps(manifest, ensure_ascii=False) + '\n'
            + self.inline_payload + '\n'
            '[UPSTREAM SIGNATURES REQUIRING CALLER REVIEW]\n'
            + json.dumps(signatures, ensure_ascii=False) + '\n'
            '[REQUIRED READS — COMPLETE IN ORDER]\n'
            '1. Read the inline upstream patch, every hunk contract and every mapping before editing.\n'
            '2. Before editing, call query_api_context with record_type="all" for each NOT INLINED '
            'symbol: ' + required + '. Read both definitions and references. If total exceeds '
            'returned, continue each record_type using its next_offset, or query exact filters. '
            'All candidate records must be delivered; overlapping pages count only once. '
            'Oversized pointers require viewcode reads of their full range at the stated ref. '
            'validate rejects incomplete required API reads.\n'
            '3. Before changing any definition or caller, use viewcode at the CURRENT cumulative '
            'target ref for that exact path/range. Read the complete signature, receiver and guard; '
            'an inventory record is only a syntactic candidate.\n'
            '4. Before validate, inspect every relevant prior-change ref marked omitted in the ledger, '
            'then recheck all changed signatures and old-arity callers against the current target.\n'
            '5. Submit the complete candidate with validate. Read its fresh CANDIDATE_API_FACTS and '
            'resolve every remaining relevant old-signature caller before finishing.\n'
        )
