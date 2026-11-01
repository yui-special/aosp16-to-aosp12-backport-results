"""Deterministic source inventories and narrow structural checks.

Calls are candidates, not a resolved type graph. Only provable newly introduced
Java duplicate declarations are blocking; control-flow deltas are review facts.
"""
from collections import Counter
import json
import re
from pathlib import Path

from tools.source_structure import (Source, child, typed_child, walk, snapshot, hunk_coordinates,
                                    hunk_evidence, resolve_ref, signature_changes, normalize_symbol)
from tools.utils import split_patch_indexed


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
                names.update(d['name'] for d in facts['scopes'] if d['kind'] == 'function')
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
    """Called immediately after applying this patch; compares to its exact base."""
    _, hunks = split_patch_indexed(patch, False)
    paths = {tuple(side[0] for side in hunk_coordinates(h)) for h in hunks}
    result = {"errors": [], "files": [], "signature_changes": []}
    for old_path, new_path in sorted(paths):
        path = Path(old_path if new_path == '/dev/null' else new_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Expected a relative candidate path")
        before = snapshot(repo, ref, old_path)
        after = Source(str(path), b'' if new_path == '/dev/null' else (Path(repo) / path).read_bytes())
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


def candidate_api_inventory(index, ref, patch, changes):
    """Overlay this applied candidate on its fixed base, including untouched callers."""
    _, hunks = split_patch_indexed(patch, False)
    paths = {coord[0] for h in hunks for coord in hunk_coordinates(h)} - {'/dev/null'}
    sources = {p: Source(p, (Path(index.repo) / p).read_bytes()) for p in paths
               if (Path(index.repo) / p).is_file()}
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
