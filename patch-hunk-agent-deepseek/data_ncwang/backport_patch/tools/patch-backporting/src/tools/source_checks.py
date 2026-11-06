"""Deterministic source inventories and narrow structural checks.

Calls are candidates, not a resolved type graph. Only provable newly introduced
Java duplicate declarations are blocking; control-flow deltas are review facts.
"""
from collections import Counter
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from tools.source_structure import (Source, child, typed_child, walk, snapshot, hunk_coordinates,
                                    hunk_evidence, resolve_ref, signature_changes, normalize_symbol)
from tools.utils import split_patch_indexed


def artifact_tree(repo, base, patch, *, strict=False):
    """Apply to a private Git index without touching the checkout or real index."""
    if strict:
        _check_local_context(repo, base, patch)
    with tempfile.TemporaryDirectory(prefix='backport-review-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(directory, 'index'), LC_ALL='C')
        command = ['git', '-C', str(repo)]
        subprocess.run(command + ['read-tree', base], env=env, check=True,
                       capture_output=True, timeout=60)
        blob = patch if patch.endswith('\n') else patch + '\n'
        # --recount can consume the next ---/+++ file header as hunk content in
        # valid traditional multi-file diffs. Honor correct counts first.
        applied = subprocess.run(command + ['apply', '--cached', '--verbose', '-'],
                       input=blob, text=True, env=env, capture_output=True, timeout=60)
        if applied.returncode:
            subprocess.run(command + ['read-tree', base], env=env, check=True,
                           capture_output=True, timeout=60)
            applied = subprocess.run(command + ['apply', '--cached', '--recount', '--verbose', '-'],
                           input=blob, text=True, env=env, check=True, capture_output=True, timeout=60)
        if strict and re.search(r'\boffset [+-]?\d+ lines?\b', applied.stderr):
            raise ValueError('Local edit needs relocated context; read the exact candidate lines and retry.')
        return subprocess.check_output(command + ['write-tree'], env=env,
                                       text=True, timeout=60).strip()


def _check_local_context(repo, tree, patch):
    """Verify old-side coordinates before Git can relocate a local repair hunk."""
    _, hunks = split_patch_indexed(patch, True)
    if len(hunks) != len(re.findall(r'^@@ ', patch, re.M)):
        raise ValueError('Unsupported local hunk header; use ordinary unified diff paths and context.')
    sources = {}
    for hunk in hunks:
        rows = hunk['text'].splitlines()
        header = next(i for i, row in enumerate(rows) if row.startswith('@@'))
        match = re.match(r'@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@', rows[header])
        if not match:
            raise ValueError('Invalid local hunk header')
        old = [row[1:] for row in rows[header+1:] if row.startswith((' ', '-'))]
        if rows[0] == '--- /dev/null':
            if old:
                raise ValueError('New file hunk contains old source')
            continue
        path = hunk['file']
        if path not in sources:
            sources[path] = subprocess.check_output(['git', '-C', str(repo), 'show', tree + ':' + path],
                timeout=60).decode('utf-8', 'surrogateescape').splitlines()
        start = max(0, int(match[1]) - (1 if old else 0))
        if start > len(sources[path]) or sources[path][start:start+len(old)] != old:
            raise ValueError('Local context mismatch at %s:%s; read this candidate range and retry.' %
                             (path, match[1]))


def candidate_diff(repo, base, tree):
    """Use Git's applied coordinates, not the submitted patch's approximate headers."""
    return subprocess.check_output([
        'git', '-C', str(repo), 'diff', '--binary', '--full-index', '--no-color',
        '--no-ext-diff', '--no-textconv', '--no-renames', '--no-relative',
        '--src-prefix=a/', '--dst-prefix=b/', '--unified=3', '--inter-hunk-context=0',
        base, tree, '--'], timeout=60).decode('utf-8', 'replace')


def compiler_diagnostics(output, max_chars=16000):
    """Retain diagnostic blocks, including javac required/found/symbol details."""
    lines = re.sub(r'\x1b\[[0-9;]*m', '', output).splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if not re.search(r'\berror(?:\[[^]]+\])?:', lines[i], re.I):
            i += 1
            continue
        block = [lines[i]]
        i += 1
        while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
            if len(block) < 40:
                block.append(lines[i])
            elif len(block) == 40:
                block.append('[diagnostic continuation truncated]')
            i += 1
        blocks.append('\n'.join(block).rstrip())
    text = '\n\n'.join(blocks) if blocks else '\n'.join(lines[-40:])
    if len(text) > max_chars:
        text = text[:max_chars] + '\n[diagnostics truncated; full build log retained]'
    return text


def infrastructure_failure(output):
    """Only explicit infrastructure signatures; unknown failures remain unclassified."""
    patterns = {
        'disk_full': r'(?m)^(?:[^\n]*:\s+)?No space left on device\s*$',
        'device_offline': r'(?m)^(?:adb: (?:error: )?|error: )(?:device offline|no devices/emulators found)\s*$',
        'dns_failure': r'(?:curl: \(6\)|Could not resolve host:|Temporary failure resolving)',
        'missing_command': r'(?m)^[^\n]*: (?:line \d+: )?[^\n:]+: command not found\s*$',
    }
    for kind, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            return dict(kind=kind, evidence=match.group(0).strip()[:500])
    return None


def candidate_diagnostic_context(repo, tree, diagnostics):
    """Map compiler locations to a fixed candidate; ambiguous generated paths stay explicit."""
    result = dict(candidate_tree=tree, frames=[], unresolved=[])
    if not tree:
        return dict(result, unavailable='No validation candidate tree was recorded')
    paths = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-r', '--name-only', tree],
                                    text=True, timeout=60).splitlines()
    locations = list(dict.fromkeys(re.findall(
        r'([^\s:]+\.(?:java|kt|c|cc|cpp|h)):(\d+)(?::\d+)?:', diagnostics)))
    budget = 12000
    for filename, raw_line in locations[:6]:
        matches = [p for p in paths if filename == p or filename.endswith('/' + p)]
        if not matches:
            matches = [p for p in paths if p.rsplit('/', 1)[-1] == filename.rsplit('/', 1)[-1]]
        if len(matches) != 1:
            result['unresolved'].append(dict(file=filename, line=int(raw_line),
                reason='ambiguous_path' if matches else 'source_absent'))
            continue
        path, line = matches[0], int(raw_line)
        lines = snapshot(repo, tree, path).data.decode('utf-8', 'replace').splitlines()
        if not 1 <= line <= len(lines):
            result['unresolved'].append(dict(path=path, line=line, reason='line_outside_candidate'))
            continue
        start, end = max(1, line - 8), min(len(lines), line + 8)
        quote = '\n'.join(f'{n}: {lines[n-1]}' for n in range(start, end + 1))
        if len(quote) > budget:
            result['truncated'] = True
            break
        result['frames'].append(dict(path=path, line=line, start_line=start, end_line=end,
                                     quote=quote, scope_complete=False, quote_complete=True,
                                     location_mapping='compiler line; generated-source offsets are not verified'))
        budget -= len(quote)
    if len(locations) > 6:
        result['truncated'] = True
    return result


def candidate_failure_context(repo, tree, output, max_frames=6, max_lines=120):
    """Resolve JVM traceback locations against the candidate, never the checkout.

    These are source facts, not a diagnosis. Ambiguous paths and truncated scopes
    stay explicit so callers cannot mistake an incomplete excerpt for proof.
    """
    result = {'candidate_tree': tree, 'frames': [], 'unresolved': []}
    if not tree:
        result['unavailable'] = 'No validation candidate tree was recorded'
        return result
    pattern = re.compile(
        r'^\s*at\s+(?:[^\s/]+/)?([\w.$]+)\.([\w$<>]+)'
        r'\(([^():/\s]+\.(?:java|kt)):(\d+)\)', re.MULTILINE)
    frames = list(dict.fromkeys(pattern.findall(output)))
    if len(frames) > 64:
        result['truncated'] = True
        frames = frames[:64]
    if not frames:
        return result
    paths = subprocess.check_output([
        'git', '-C', str(repo), 'ls-tree', '-r', '--name-only', '-z', tree,
    ], timeout=60).decode('utf-8', 'surrogateescape').split('\0')
    remaining = 24000
    for owner, method, filename, raw_line in frames:
        package = owner.rpartition('.')[0].replace('.', '/')
        suffix = package + '/' + filename if package else filename
        matches = [p for p in paths if p == suffix or p.endswith('/' + suffix)]
        if len(matches) != 1:
            result['unresolved'].append({'frame': owner + '.' + method,
                                         'file': filename, 'line': int(raw_line),
                                         'reason': 'ambiguous_path' if matches else 'source_absent'})
            continue
        if len(result['frames']) >= max_frames or remaining <= 0:
            result['truncated'] = True
            break
        path, line = matches[0], int(raw_line)
        source = snapshot(repo, tree, path)
        lines = source.data.decode('utf-8', 'replace').splitlines()
        if not 1 <= line <= len(lines):
            result['unresolved'].append({'path': path, 'line': line,
                                         'reason': 'line_outside_candidate'})
            continue
        scopes = [d for d in source.declarations if d['kind'] in {'function', 'lambda'}
                  and d['start'] <= line <= d['end']]
        scope = min(scopes, key=lambda d: d['end'] - d['start']) if scopes else None
        start, end = ((scope['start'], scope['end']) if scope else
                      (max(1, line - 12), min(len(lines), line + 12)))
        if end - start + 1 > max_lines:
            start = max(start, line - max_lines // 2)
            end = min(end, start + max_lines - 1)
        quote = '\n'.join(f'{i}: {lines[i-1]}' for i in range(start, end + 1))
        complete = bool(scope and start == scope['start'] and end == scope['end']
                        and len(quote) <= remaining)
        quote_complete = len(quote) <= remaining
        quote = quote[:remaining]
        remaining -= len(quote)
        frame = {'path': path, 'line': line, 'method': method,
                 'start_line': start, 'end_line': end,
                 'scope_complete': complete, 'parse_status': source.status, 'quote': quote,
                 'quote_complete': quote_complete}
        # Expose the concrete subcase arguments and neighbouring invocations.
        # Same callee/receiver spelling is a syntactic relation, not type binding
        # or proof that an earlier source statement ran before the failing call.
        if scope:
            calls = [c for c in source.references() if c['scope'] == scope['qualified_name']
                     and scope['start'] <= c['line'] <= c['end_line'] <= scope['end']]
            at_line = [c for c in calls if c['line'] <= line <= c['end_line']]
            at_line.sort(key=lambda c: c['end_line'] - c['line'])
            frame['calls_at_location'] = []
            if len(at_line) > 3:
                frame['calls_truncated'] = True
            for call in at_line[:3]:
                previous = [c for c in calls if c['end_line'] < call['line']
                            and (c['callee'], c['receiver']) == (call['callee'], call['receiver'])]
                if len(previous) > 3:
                    frame['calls_truncated'] = True
                records = [dict(relation='at_traceback', call=call)] + [
                    dict(relation='preceding_same_scope_call', call=c)
                    for c in sorted(previous, key=lambda c: c['line'])[-3:]]
                for record in records:
                    call = record.pop('call')
                    record.update({k: call[k] for k in (
                        'line', 'end_line', 'callee', 'receiver', 'arguments', 'expression')})
                    size = len(json.dumps(record, ensure_ascii=False))
                    if size > remaining:
                        frame['calls_truncated'] = True
                        continue
                    remaining -= size
                    frame['calls_at_location'].append(record)
            frame['call_order'] = 'source order only; execution and receiver binding are not proven'
        result['frames'].append(frame)
    return result


class ApiCatalog:
    """Whole-patch signatures, with lazy target expansion through syntactic calls."""
    def __init__(self, index, parent, commit, hunks):
        self.index = index
        parent, commit = resolve_ref(index.repo, parent), resolve_ref(index.repo, commit)
        self.evidence = {h['idx']: hunk_evidence(index.repo, parent, commit, h) for h in hunks}
        self.changes = [dict(c, hunk=h['idx'], path=h['file']) for h in hunks
                        for c in self.evidence[h['idx']]['signature_changes']]
        self.names = {c['name'] for c in self.changes}

    def inventory(self, target, hunks, target_symbols=()):
        target = resolve_ref(self.index.repo, target)
        names, diagnostics = set(target_symbols), []
        for h in hunks:
            evidence = self.evidence[h['idx']]
            names.update(c['name'] for c in evidence['signature_changes'])
            for side in ('before', 'after'):
                facts = evidence[side]
                names.update(d['name'] for d in facts['scopes']
                             if d['kind'] == 'function' and d['name'] in self.names)
                names.update(re.split(r'[.:>]', c['callee'])[-1] for c in facts['calls']
                             if re.split(r'[.:>]', c['callee'])[-1] in self.names)
                if facts['status'] not in {'ok', 'missing'}:
                    diagnostics.append({'hunk': h['idx'], 'side': side, 'status': facts['status']})
        names = {normalize_symbol(name).rsplit('.', 1)[-1] for name in names}
        apis, edges, pending, visited = [], [], sorted(names), set()
        calls_by_path = {}
        while pending:
            name = pending.pop(0)
            if name in visited:
                continue
            visited.add(name)
            api = self.index.inspect(target, name)
            apis.append(api)
            for definition in api['definitions']:
                path = definition['path']
                if path not in calls_by_path:
                    calls_by_path[path] = snapshot(self.index.repo, target, path).references()
                for call in calls_by_path[path]:
                    callee = re.split(r'[.:>]', call['callee'])[-1]
                    if (definition['kind'] == 'function' and callee in self.names
                            and definition['start'] <= call['line'] <= definition['end']):
                        edge = {'from': name, 'to': callee, 'call': call}
                        if edge not in edges:
                            edges.append(edge)
                        if callee not in visited:
                            pending.append(callee)
        return {'apis': apis, 'related_api_candidates': edges,
                'upstream_signature_changes': [c for c in self.changes if c['name'] in visited],
                'changed_api_names': sorted(self.names), 'source_diagnostics': diagnostics,
                'resolution': 'syntactic candidates, not a type-resolved call graph'}


def api_inventory(index, target, parent, commit, hunks):
    return ApiCatalog(index, parent, commit, hunks).inventory(target, hunks)


def _java_issues(source):
    """Exact syntactic duplicates in named class bodies; no type-erasure guessing."""
    if source.language != "java" or source.status != "ok":
        return []
    signatures, issues = {}, []
    for declaration in source.declarations:
        node = declaration["_node"]
        if (declaration["kind"] != "function" or node.parent is None
                or node.parent.type not in {"class_body", "interface_body", "enum_body_declarations"}
                or node.parent.parent.type == "object_creation_expression"):
            continue
        parameters = child(node, "parameters")
        types, names = [], []
        for parameter in parameters.named_children if parameters else []:
            if "comment" in parameter.type:
                continue
            name = child(parameter, "name")
            if name is None and parameter.type == "spread_parameter":
                declarator = typed_child(parameter, "variable_declarator")
                name = child(declarator, "name") if declarator else None
            if name is None:  # Varargs/receiver forms need their own grammar handling.
                types = None
                continue
            names.append(source.text(name))
            typ = child(parameter, "type")
            if typ is None:
                types = None
            elif types is not None:
                # Array suffixes belong to the type, including those after the name.
                dimensions = child(parameter, "dimensions")
                types.append(source.text(typ) + source.text(dimensions))
        for name, count in Counter(names).items():
            if count > 1:
                issues.append({"kind": "duplicate_parameter", "scope": declaration["qualified_name"],
                               "signature": declaration["signature"], "name": name,
                               "line": declaration["start"]})
        if types is None or child(node, "type_parameters") is not None:
            continue
        # Byte offsets distinguish separate/anonymous type bodies within a file.
        key = (node.parent.id, node.type, declaration["name"], tuple(types))
        signatures.setdefault(key, []).append(declaration)
    for key, declarations in signatures.items():
        for declaration in declarations[1:]:
            issues.append({"kind": "duplicate_method", "scope": declaration["qualified_name"],
                           "parameter_types": list(key[-1]), "node_type": key[1],
                           "line": declaration["start"]})
    return issues


def _control_flow(source):
    if source.status != "ok":
        return []
    rows = []
    for node in walk(source.tree.root_node):
        if node.type != "if_statement":
            continue
        ancestors = []
        branch, parent = node, node.parent
        while parent is not None:
            if parent.type == "if_statement":
                alternative = child(parent, "alternative")
                ancestors.append({"condition": source.text(child(parent, "condition")),
                                  "branch": "else" if alternative == branch else "then"})
            branch, parent = parent, parent.parent
        owners = [d for d in source.declarations if d["kind"] == "function"
                  and d["_node"].start_byte <= node.start_byte < d["_node"].end_byte]
        owner = min(owners, key=lambda d: d["end"]-d["start"]) if owners else None
        rows.append({"scope": owner["qualified_name"] if owner else "",
                     "condition": source.text(child(node, "condition")),
                     "ancestors": list(reversed(ancestors)),
                     "has_else": child(node, "alternative") is not None,
                     "line": node.start_point.row + 1})
    return rows


def _new_records(before, after):
    key = lambda row: json.dumps({k: v for k, v in row.items() if k != "line"}, sort_keys=True)
    remaining = Counter(key(row) for row in before)
    result = []
    for row in after:
        identity = key(row)
        if remaining[identity]:
            remaining[identity] -= 1
        else:
            result.append(row)
    return result


def structural_delta(before, after):
    old_flow, new_flow = _control_flow(before), _control_flow(after)
    return {"errors": _new_records(_java_issues(before), _java_issues(after)),
            "control_flow": {"removed": _new_records(new_flow, old_flow),
                             "added": _new_records(old_flow, new_flow)},
            "parse_status": {"before": before.status, "after": after.status}}


def inspect_worktree_patch(repo, ref, patch):
    """Inspect the applied artifact against its base, independently of patch offsets."""
    tree = artifact_tree(repo, ref, patch)
    _, hunks = split_patch_indexed(candidate_diff(repo, ref, tree), False)
    paths = {tuple(side[0] for side in hunk_coordinates(h)) for h in hunks}
    result = {"errors": [], "files": [], "signature_changes": [], "candidate_tree": tree}
    for old_path, new_path in sorted(paths):
        path = Path(old_path if new_path == '/dev/null' else new_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Expected a relative candidate path")
        before = snapshot(repo, ref, old_path)
        after = snapshot(repo, tree, new_path)
        delta = structural_delta(before, after)
        changed = {'before': set(), 'after': set()}
        for hunk in hunks:
            coords = hunk_coordinates(hunk)
            if (coords[0][0], coords[1][0]) == (old_path, new_path):
                for side, coord in zip(('before', 'after'), coords):
                    changed[side].update(coord[3])
        evidence = {side: {'scopes': source.declarations, 'changed_lines': changed[side]}
                    for side, source in (('before', before), ('after', after))}
        result['signature_changes'].extend(dict(c, path=str(path))
                                            for c in signature_changes(before, after, evidence))
        result["errors"].extend(dict(error, path=new_path) for error in delta["errors"])
        if delta["control_flow"]["added"] or delta["control_flow"]["removed"] or after.status != "ok":
            result["files"].append(dict(delta, path=new_path))
    return result


def candidate_api_inventory(index, ref, patch, changes, *, tree=None):
    """Overlay this applied candidate on its fixed base, including untouched callers."""
    tree = tree or artifact_tree(index.repo, ref, patch)
    _, hunks = split_patch_indexed(candidate_diff(index.repo, ref, tree), False)
    paths = {coord[0] for h in hunks for coord in hunk_coordinates(h)} - {'/dev/null'}
    sources = {p: snapshot(index.repo, tree, p) for p in paths}
    sources = {p: source for p, source in sources.items() if source.status != 'missing'}
    result = []
    for name in sorted({c['name'] for c in changes}):
        base = index.inspect(ref, name)
        row = dict(base, candidate='applied patch over ref; not committed yet')
        row['definitions'] = [d for d in base['definitions'] if d['path'] not in paths]
        row['references'] = [r for r in base['references'] if r['path'] not in paths]
        row['diagnostics'] = [d for d in base['diagnostics'] if d['path'] not in paths]
        for p, source in sources.items():
            row['definitions'].extend(dict(d, path=p) for d in source.definitions(name))
            row['references'].extend(source.references(name))
            if source.status != 'ok':
                row['diagnostics'].append({'path': p, 'status': source.status})
        result.append(row)
    return result
