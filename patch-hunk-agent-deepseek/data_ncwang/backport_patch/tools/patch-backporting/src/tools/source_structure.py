"""Tree-sitter source evidence shared by grouping and symbol navigation.

Parse immutable Git blobs, never diff fragments or the current checkout. Syntax
facts are not a type-resolved call graph. Unsupported/partial parses stay visible.
"""
import ctypes
import hashlib
from functools import lru_cache
import importlib
import json
import os
from pathlib import Path
import re
import subprocess


EXTENSIONS = {
    ".java": "java", ".aidl": "aidl", ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".rs": "rust", ".py": "python",
}
FUNCTIONS = {"method_declaration", "constructor_declaration", "function_definition",
             "function_declaration", "function_item", "function_signature_item"}
TYPES = {"class_declaration", "interface_declaration", "enum_declaration",
         "record_declaration", "struct_specifier", "class_specifier", "union_specifier",
         "enum_specifier", "class_definition", "struct_item", "enum_item", "trait_item",
         "object_declaration", "parcelable_declaration"}
IDENTIFIERS = {"identifier", "field_identifier", "type_identifier", "simple_identifier"}
CALLS = {"method_invocation", "call_expression", "call", "object_creation_expression"}


@lru_cache(maxsize=8)
def _read_config(path, stamp):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}


def settings():
    path = os.environ.get("SOURCE_STRUCTURE_CONFIG", "")
    return _read_config(path, Path(path).stat().st_mtime_ns if path else 0)


def language_for(path):
    return settings().get("extensions", {}).get(Path(path).suffix.lower(),
                                                EXTENSIONS.get(Path(path).suffix.lower()))


@lru_cache(maxsize=32)
def _language(name, spec_json):
    from tree_sitter import Language
    spec = json.loads(spec_json)
    if "library" in spec:
        library = ctypes.CDLL(spec["library"])
        function = getattr(library, spec.get("symbol", "tree_sitter_" + name))
        function.restype = ctypes.c_void_p
        capsule = ctypes.pythonapi.PyCapsule_New
        capsule.restype = ctypes.py_object
        capsule.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        return Language(capsule(function(), b"tree_sitter.Language", None)), library
    module = importlib.import_module(spec.get("module", "tree_sitter_" + name))
    return Language(getattr(module, spec.get("function", "language"))()), module


def walk(node):
    """Iterative traversal also handles very large generated C files."""
    cursor = node.walk()
    while True:
        yield cursor.node
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return


def child(node, *fields):
    for field in fields:
        result = node.child_by_field_name(field)
        if result is not None:
            return result
    return None


def typed_child(node, *types):
    return next((n for n in node.named_children if n.type in types), None)


def declarator_name(node):
    """Follow declarator fields; parameter identifiers are not function names."""
    while node is not None:
        if node.type in IDENTIFIERS or node.type in {"operator_name", "destructor_name"}:
            return node
        next_node = child(node, "name", "declarator")
        if next_node is None:
            return None
        node = next_node
    return None


class Source:
    def __init__(self, path, data):
        self.path, self.data = path, data
        self.language = language_for(path)
        self.tree = None
        self.declarations = []
        self.status = "unsupported"
        self.reason = "No grammar configured for this extension"
        if not self.language:
            return
        spec = settings().get("grammars", {}).get(self.language, {})
        # AIDL is deliberately not parsed as Java.
        if self.language == "aidl" and not spec:
            self.reason = "Configure an AIDL grammar in SOURCE_STRUCTURE_CONFIG"
            return
        from tree_sitter import Parser
        try:
            language, _owner = _language(self.language, json.dumps(spec, sort_keys=True))
        except (ImportError, OSError, AttributeError, ValueError) as exc:
            raise RuntimeError("Tree-sitter grammar unavailable for %s: %s" %
                               (self.language, type(exc).__name__)) from exc
        self.tree = Parser(language).parse(data)
        self.status = "partial" if self.tree.root_node.has_error else "ok"
        self.reason = "Syntax recovery used" if self.status == "partial" else ""
        self._collect_declarations()

    def text(self, node):
        return self.data[node.start_byte:node.end_byte].decode("utf-8", "replace") if node else ""

    def without_casts(self, node):
        """Expression text with syntactic casts removed; never edits source code."""
        if node.type == "cast_expression":
            value = child(node, "value")
            if value is not None:
                return self.without_casts(value)
        cursor, parts = node.start_byte, []
        for nested in node.named_children:
            parts.append(self.data[cursor:nested.start_byte].decode("utf-8", "replace"))
            parts.append(self.without_casts(nested))
            cursor = nested.end_byte
        parts.append(self.data[cursor:node.end_byte].decode("utf-8", "replace"))
        return "".join(parts)

    def _collect_declarations(self):
        stack = [(self.tree.root_node, (), False)]
        while stack:
            node, owners, in_function = stack.pop()
            kind, name_node = None, None
            if node.type in {"namespace_definition", "impl_item"}:
                owner = child(node, "name", "type")
                if owner:
                    owners = (*owners, self.text(owner))
            if node.type in FUNCTIONS | TYPES:
                kind = "function" if node.type in FUNCTIONS else "type"
                name_node = declarator_name(node)
            elif node.type in {"lambda_expression", "lambda_literal"}:
                kind = "lambda"
                parent = node.parent
                if parent and parent.type in {"variable_declarator", "init_declarator"}:
                    name_node = declarator_name(parent)
                if name_node is None:
                    name_node = node
            elif node.type == "function_declarator" and not in_function:
                kind, name_node = "function", declarator_name(node)
            elif node.type in {"variable_declarator", "init_declarator", "variable_declaration",
                               "type_definition", "preproc_def", "preproc_function_def",
                               "field_declaration", "enum_constant", "enumerator", "const_item",
                               "static_item", "type_item", "field_declaration_list"}:
                if not in_function:
                    kind, name_node = "symbol", declarator_name(node)
                    if name_node is None and node.type == "variable_declaration":
                        name_node = typed_child(node, *IDENTIFIERS)
            if name_node is not None:
                name = ("<lambda@%d:%d>" % (node.start_point.row + 1, node.start_point.column + 1)
                        if name_node == node else self.text(name_node))
                qualified_name = ".".join((*owners, name))
                if name_node.parent and name_node.parent.type in {"qualified_identifier", "scoped_identifier"}:
                    qualified_name = self.text(name_node.parent).replace("::", ".")
                body = child(node, "body") or typed_child(node, "function_body", "class_body")
                params = child(node, "parameters") or typed_child(node, "function_value_parameters")
                if params is None and node.type == "function_definition":
                    decl = child(node, "declarator")
                    while decl is not None and decl.type != "function_declarator":
                        decl = child(decl, "declarator")
                    params = child(decl, "parameters") if decl else None
                signature_end = body.start_byte if body else node.end_byte
                signature_bytes = self.data[node.start_byte:signature_end].rstrip()
                signature = signature_bytes.decode("utf-8", "replace").strip()
                parameter_nodes = [p for p in params.named_children if "comment" not in p.type] if params else []
                parameter_names = []
                for p in parameter_nodes:
                    pn = (declarator_name(p) or
                          declarator_name(typed_child(p, "variable_declarator")) or
                          typed_child(p, "identifier", "simple_identifier"))
                    if pn:
                        parameter_names.append(self.text(pn))
                record = {"name": name, "qualified_name": qualified_name,
                          "language": self.language,
                          "kind": kind, "node_type": node.type,
                          "start": node.start_point.row + 1, "end": node.end_point.row + 1,
                          "name_line": name_node.start_point.row + 1,
                          "signature": signature, "parameters": [self.text(p) for p in parameter_nodes],
                          "parameter_types": [self.text(child(p, "type") or typed_child(p, "user_type", "nullable_type")) for p in parameter_nodes],
                          "has_body": body is not None,
                          "parameter_names": parameter_names,
                          "signature_end": node.start_point.row + 1 + signature_bytes.count(b"\n"),
                          "parse_error": node.has_error, "_node": node}
                self.declarations.append(record)
                if kind in {"function", "type", "lambda"}:
                    owners = (*owners, name)
                    in_function = in_function or kind in {"function", "lambda"}
            stack.extend((n, owners, in_function) for n in reversed(node.named_children))

    def symbols(self):
        return [(d["name"], d["name_line"]) for d in self.declarations if d["kind"] != "lambda"]

    @lru_cache(maxsize=8)
    def java_imports(self):
        """Explicit type imports only; shadowing/ambiguity prevents automatic edges."""
        imports, shadowed = {}, set()
        if self.language != "java" or self.status != "ok":
            return imports, shadowed
        for node in walk(self.tree.root_node):
            if node.type == "import_declaration":
                match = re.fullmatch(r"import\s+([\w.]+)\s*;", self.text(node).strip())
                if match:
                    qualified = match[1]
                    imports.setdefault(qualified.rsplit(".", 1)[-1], []).append({
                        "qualified_name": qualified, "line": node.start_point.row + 1,
                        "text": self.text(node)})
            elif node.type in TYPES | {"variable_declarator", "formal_parameter", "type_parameter"}:
                name = declarator_name(node) or typed_child(node, "type_identifier")
                if name:
                    shadowed.add(self.text(name))
        return imports, shadowed

    def evidence(self, changed_lines, start, count):
        """Scopes contain changed lines; context-only neighbours do not own a hunk."""
        lines = set(changed_lines)
        scope_lines = lines or set(range(max(1, start), start + count))
        scopes = [d for d in self.declarations if d["kind"] in {"function", "type", "lambda"}
                  and any(d["start"] <= line <= d["end"] for line in scope_lines)]
        ancestors = [{k: d[k] for k in ("qualified_name", "start", "end", "parameter_names")}
                     for d in scopes if d["kind"] in {"function", "lambda"}]
        # Keep innermost owners, while retaining multiple methods touched by one hunk.
        scopes = [d for d in scopes if any(
            d["start"] <= line <= d["end"] and not any(
                q is not d and q["start"] >= d["start"] and q["end"] <= d["end"]
                and (q["start"], q["end"]) != (d["start"], d["end"])
                and q["start"] <= line <= q["end"] for q in scopes) for line in scope_lines)]
        result = {"path": self.path, "language": self.language, "status": self.status,
                  "range": [start, count], "changed_lines": sorted(lines),
                  "scope_basis": "changed_lines" if lines else "context_only",
                  "scopes": [], "enclosing_functions": ancestors, "calls": [], "identifiers": [],
                  "imports": [], "import_uses": [], "statements": [],
                  "writes": [], "declarations": [{k: d[k] for k in ("name", "qualified_name", "signature", "kind")}
                     for d in self.declarations if d["kind"] == "symbol" and d["name_line"] in lines]}
        if self.reason:
            result["reason"] = self.reason
        for d in scopes:
            entry = {k: v for k, v in d.items() if not k.startswith("_")}
            entry["content_sha256"] = hashlib.sha256(self.data[d["_node"].start_byte:d["_node"].end_byte]).hexdigest()
            entry["signature_changed"] = any(d["start"] <= n <= d["signature_end"] for n in lines)
            result["scopes"].append(entry)
        if self.tree:
            imports, shadowed = self.java_imports()
            result["imports"] = [entry for entries in imports.values() for entry in entries
                                 if entry["line"] in lines]
            identifiers = set()
            stack = [self.tree.root_node]
            while stack:
                n = stack.pop()
                if not any(n.start_point.row + 1 <= line <= n.end_point.row + 1 for line in lines):
                    continue
                if n.type in IDENTIFIERS:
                    name = self.text(n)
                    identifiers.add(name)
                    # A qualified use, string/comment or shadowed name is not proof
                    # that this import supplies the identifier. Static/wildcard
                    # imports and other languages remain semantic-review work.
                    entries = imports.get(name, [])
                    if (len(entries) == 1 and name not in shadowed and n.parent
                            and n.parent.type not in {"scoped_identifier", "scoped_type_identifier",
                                                      "field_access", "import_declaration"}
                            and (n.type == "type_identifier" or
                                 (n.parent.type == "method_invocation" and child(n.parent, "object") == n))):
                        result["import_uses"].append({"qualified_name": entries[0]["qualified_name"],
                                                     "line": n.start_point.row + 1, "identifier": name})
                if n.type in {"expression_statement", "return_statement", "assert_statement",
                              "throw_statement", "local_variable_declaration"}:
                    # Keep the complete changed statement: an assertion's receiver
                    # often lies on an unchanged line above its new expected value.
                    result["statements"].append({"line": n.start_point.row + 1,
                                                "end": n.end_point.row + 1, "text": self.text(n)})
                if n.type in CALLS:
                    name = child(n, "name", "function", "type")
                    args = child(n, "arguments") or typed_child(n, "value_arguments")
                    if name is None and n.type == "call_expression":
                        name = n.named_children[0] if n.named_children else None
                    result["calls"].append({"expression": self.text(n), "callee": self.text(name),
                        "receiver": self.text(child(n, "object")),
                        "arguments": [self.text(a) for a in args.named_children if "comment" not in a.type] if args else [],
                        "line": n.start_point.row + 1, "parse_error": n.has_error})
                if n.type in {"assignment_expression", "variable_declarator", "init_declarator"}:
                    target = child(n, "left", "name", "declarator")
                    value = child(n, "right", "value")
                    if target and value:
                        result["writes"].append({"target": self.text(target), "value": self.text(value),
                                                 "operator": self.text(child(n, "operator")) or "=",
                                                 "value_without_casts": re.sub(r"\s+", "", self.without_casts(value)),
                                                 "line": n.start_point.row + 1})
                stack.extend(reversed(n.named_children))
            result["identifiers"] = sorted(identifiers)
        config = settings()
        padding = max(0, int(config.get("context_lines", 3)))
        limit = max(1, int(config.get("max_context_lines", 80)))
        source_lines = self.data.decode("utf-8", "replace").splitlines()
        wanted = sorted({i for line in lines for i in range(max(1, line-padding),
                         min(len(source_lines), line+padding)+1)})
        result["context"] = [{"line": n, "text": source_lines[n-1]} for n in wanted[:limit]]
        result["context_truncated"] = len(wanted) > limit
        return result


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def resolve_ref(repo, ref):
    return git(repo, "rev-parse", "--verify", ref + "^{commit}").decode().strip()


@lru_cache(maxsize=8)
def _snapshot(repo, sha, path, config_stamp):
    if path == "/dev/null":
        source = Source(path, b"")
        source.status, source.reason = "missing", "Diff marks this side as absent"
        return source
    try:
        data = git(repo, "show", sha + ":" + path)
    except subprocess.CalledProcessError:
        # Missing/renamed/new/deleted files are distinct from an unavailable repo/ref.
        if git(repo, "ls-tree", sha, "--", path):
            raise
        source = Source(path, b"")
        source.status, source.reason = "missing", "File absent at this revision"
        return source
    return Source(path, data)


def snapshot(repo, sha, path):
    return _snapshot(os.path.realpath(repo), sha, path, json.dumps(settings(), sort_keys=True))


def hunk_coordinates(hunk):
    """Track old/new lines independently, including zero-length insert/delete hunks."""
    old_path = new_path = hunk["file"]
    old_start = new_start = old_count = new_count = 0
    old_lines, new_lines = [], []
    active = False
    for line in hunk["text"].splitlines():
        if not active and line.startswith("--- "):
            old_path = line[4:].removeprefix("a/")
        elif not active and line.startswith("+++ "):
            new_path = line[4:].removeprefix("b/")
        elif line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                raise ValueError("Invalid hunk header")
            a, b, c, d = match.groups()
            old = old_start = int(a)
            new = new_start = int(c)
            old_count, new_count = int(b or 1), int(d or 1)
            active = True
        elif active:
            if line.startswith("-"):
                old_lines.append(old)
                old += 1
            elif line.startswith("+"):
                new_lines.append(new)
                new += 1
            elif line.startswith(" "):
                old += 1
                new += 1
    return ((old_path, old_start, old_count, old_lines),
            (new_path, new_start, new_count, new_lines))


def hunk_evidence(repo, parent, commit, hunk):
    result = {"before_ref": parent, "after_ref": commit}
    snapshots = []
    for key, ref, (path, start, count, lines) in zip(
            ("before", "after"), (parent, commit), hunk_coordinates(hunk)):
        source = snapshot(repo, ref, path)
        snapshots.append(source)
        source_lines = source.data.decode("utf-8", "replace").splitlines()
        position, active = start, False
        for line in hunk["text"].splitlines():
            if line.startswith("@@"):
                active = True
            elif active and line[:1] in ({" ", "-"} if key == "before" else {" ", "+"}):
                if position < 1 or position > len(source_lines) or source_lines[position-1] != line[1:]:
                    raise ValueError("Patch/source mismatch: %s %s:%s at line %d" % (key, ref, path, position))
                position += 1
        result[key] = source.evidence(lines, start, count)
    result["signature_changes"] = signature_changes(snapshots[0], snapshots[1], result)
    old_writes, new_writes = result["before"]["writes"], result["after"]["writes"]
    changes = []
    if (old_writes and len(old_writes) == len(new_writes)
            == len(result["before"]["changed_lines"]) == len(result["after"]["changed_lines"])):
        old_lines = snapshots[0].data.decode("utf-8", "replace").splitlines()
        new_lines = snapshots[1].data.decode("utf-8", "replace").splitlines()
        for old, new, old_number, new_number in zip(old_writes, new_writes,
                result["before"]["changed_lines"], result["after"]["changed_lines"]):
            old_line, new_line = old_lines[old_number - 1], new_lines[new_number - 1]
            if not (old["line"] == old_number and new["line"] == new_number
                    and old["target"] == new["target"]
                    and re.sub(r"\s+", "", old["value"]) != re.sub(r"\s+", "", new["value"])
                    and old["value_without_casts"] == new["value_without_casts"]
                    and old["value"] in old_line and new["value"] in new_line
                    and re.sub(r"\s+", "", old_line.replace(old["value"], "<value>", 1)) ==
                        re.sub(r"\s+", "", new_line.replace(new["value"], "<value>", 1))):
                break
            changes.append({"target": new["target"], "before": old["value"], "after": new["value"]})
        if len(changes) == len(old_writes):
            result["cast_only_assignment"] = changes
    return result


def parameter_types(declaration):
    aliases = {"Int": "int", "Boolean": "boolean", "Long": "long", "Byte": "byte",
               "Short": "short", "Float": "float", "Double": "double", "Char": "char"}
    types = declaration.get("parameter_types", [])
    if not types or any(not t for t in types):
        # Unknown grammars must not invent overload compatibility.
        return tuple(declaration.get("parameters", []))
    if declaration.get("language") == "kotlin":
        return tuple(aliases.get(t.strip(), t.strip()) for t in types)
    return tuple(t.strip() for t in types)


def signature_changes(before, after, evidence):
    """Match unchanged overloads first, then uniquely changed signatures.

    Replaced public methods and new compatible overloads have different effects
    on existing callers. Retaining that distinction avoids a name-wide closure.
    """
    from difflib import SequenceMatcher
    touched = {d["qualified_name"] for side in ("before", "after")
               for d in evidence[side]["scopes"] if d["kind"] == "function"}
    result = []
    for name in sorted(touched):
        old = [d for d in before.declarations if d["kind"] == "function" and d["qualified_name"] == name]
        new = [d for d in after.declarations if d["kind"] == "function" and d["qualified_name"] == name]
        # A signature still present after the patch is not incompatible.
        old_keys, new_keys = {parameter_types(d) for d in old}, {parameter_types(d) for d in new}
        removed = [d for d in old if parameter_types(d) not in new_keys]
        added = [d for d in new if parameter_types(d) not in old_keys]
        pairs = []
        for left in sorted(removed, key=lambda d: (-len(parameter_types(d)), d["start"])):
            scored = sorted(((SequenceMatcher(None, parameter_types(left), parameter_types(right)).ratio(), i)
                             for i, right in enumerate(added)), reverse=True)
            if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                right = added.pop(scored[0][1])
                pairs.append((left, right))
            else:
                pairs.append((left, None))
        pairs.extend((None, right) for right in added)
        for left, right in pairs:
            involved = any(d is not None and any(d["start"] <= n <= d["signature_end"]
                           for n in evidence[side]["changed_lines"])
                           for d, side in ((left, "before"), (right, "after")))
            if not involved:
                continue
            result.append({"name": (right or left)["name"], "scope": name,
                           "kind": "replacement" if left and right else "addition" if right else "removal",
                           "before": left["signature"] if left else None,
                           "after": right["signature"] if right else None,
                           "old_types": list(parameter_types(left)) if left else None,
                           "new_types": list(parameter_types(right)) if right else None,
                           "added_parameters": sorted(set(right["parameter_names"] if right else []) -
                                                      set(left["parameter_names"] if left else [])),
                           "old_signature_retained": bool(left and parameter_types(left) in new_keys),
                           "abstract_addition": bool(not left and right and not right["has_body"]),
                           "parse_error": any(d["parse_error"] for d in (left, right) if d)})
    return result


def structure_relations(records):
    """Scoped parameter prerequisites; name/arity call matches stay candidates."""
    signatures, relations = [], []
    for r in records:
        s = r.get("source_structure", {})
        before = s.get("before", {})
        after = s.get("after", {})
        for new in after.get("scopes", []):
            olds = [d for d in before.get("scopes", [])
                    if d["qualified_name"] == new["qualified_name"] and d["kind"] == "function"]
            if new["kind"] != "function" or not new["signature_changed"] or new["parse_error"]:
                continue
            if any(old["parse_error"] for old in olds):
                continue
            if len(olds) == 1 and olds[0]["parameters"] != new["parameters"]:
                signatures.append((r, olds[0], new))
            added = set(new["parameter_names"]) - {p for old in olds for p in old["parameter_names"]}
            for other in records:
                if r["hunk"] == other["hunk"]:
                    continue
                evidence = other.get("source_structure", {}).get("after", {})
                if evidence.get("path") != after.get("path"):
                    continue
                owners = [d for d in evidence.get("enclosing_functions", [])
                          if (d["qualified_name"], d["start"]) == (new["qualified_name"], new["start"])]
                used = sorted(added & set(evidence.get("identifiers", [])))
                if owners and used:
                    relations.append({"kind": "parameter_prerequisite", "provider": r["hunk"],
                        "consumer": other["hunk"], "scope": new["qualified_name"], "parameters": used,
                        "meaning": "Provider may be in an earlier group; this does not force merging"})
    for r, old, new in signatures:
        for other in records:
            evidence = other.get("source_structure", {})
            for side, definition in (("before", old), ("after", new)):
                for call in evidence.get(side, {}).get("calls", []):
                    tail = re.split(r"[.:>]", call["callee"])[-1]
                    if tail == new["name"] and len(call["arguments"]) == len(definition["parameters"]):
                        relations.append({"kind": "signature_call_candidate", "definition": r["hunk"],
                            "call": other["hunk"], "side": side, "scope": new["qualified_name"],
                            "expression": call["expression"],
                            "meaning": "Name/arity match only; check receiver, overload and diff before merging"})
    return relations


class SymbolIndex:
    """Lazy exact lookup; immutable SHA keys never require checkout/reset."""
    def __init__(self, repo):
        self.repo = os.path.realpath(repo)
        self.maps, self.complete, self.diagnostics = {}, set(), {}
        self.parsed, self.looked_up = set(), set()

    def _add(self, sha, path):
        if (sha, path) in self.parsed:
            return
        source = snapshot(self.repo, sha, path)
        if source.status != "ok":
            self.diagnostics.setdefault(sha, {})[path] = source.status
        mapping = self.maps.setdefault(sha, {})
        for name, line in source.symbols():
            locations = mapping.setdefault(name, [])
            if (path, line) not in locations:
                locations.append((path, line))
        self.parsed.add((sha, path))

    def locate(self, ref, symbol):
        sha = resolve_ref(self.repo, ref)
        if sha in self.complete or (sha, symbol) in self.looked_up:
            return sorted(self.maps.get(sha, {}).get(symbol, [])) or None
        # A cached symbol may be only partially populated by another name's file.
        result = subprocess.run(["git", "-C", self.repo, "grep", "-Ilz", "-F", "-w",
                                 "-e", symbol, sha, "--"], capture_output=True)
        if result.returncode not in (0, 1):
            raise RuntimeError("Git symbol candidate search failed")
        for item in result.stdout.split(b"\0"):
            if item:
                path = os.fsdecode(item).split(":", 1)[1]
                if language_for(path):
                    self._add(sha, path)
        self.looked_up.add((sha, symbol))
        return sorted(self.maps.get(sha, {}).get(symbol, [])) or None

    def prepare(self, ref):
        sha = resolve_ref(self.repo, ref)
        if sha not in self.complete:
            for path in git(self.repo, "ls-tree", "-rz", "--name-only", sha).split(b"\0"):
                if path and language_for(os.fsdecode(path)):
                    self._add(sha, os.fsdecode(path))
            self.complete.add(sha)
        return self.maps.setdefault(sha, {})
