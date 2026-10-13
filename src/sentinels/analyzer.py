"""Source-only, bounded abstract interpretation. No application imports or eval.

The interpreter follows supported bindings and retains unknown values. Its graph
is structural evidence, never a record of successful application execution.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import platform
import symtable
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from .models import (AnalysisReport, Config, ContractResult, Finding, GraphEdge,
                     SourceLocation)

from . import __version__ as VERSION
_MISSING = object()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Val:
    refs: frozenset[str] = frozenset()
    classes: frozenset[str] = frozenset()
    identities: frozenset[str] = frozenset()
    origins: frozenset[tuple[str, str]] = frozenset()
    types: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    maps: tuple[dict[str, "Val"], ...] = ()
    literal: Any = field(default=_MISSING, compare=False)
    bound: tuple[tuple[str, "Val"], ...] = ()
    pending: tuple[tuple[str, tuple["Val", ...], tuple[tuple[str, "Val"], ...], "Val | None"], ...] = ()
    future: bool = False
    apps: frozenset[str] = frozenset()


def unknown(reason: str) -> Val:
    return Val(unknown=frozenset([reason]))


def literal(value: Any) -> Val:
    kind = "null" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string" if isinstance(value, str) else "array" if isinstance(value, (list, tuple, set)) else "object"
    return Val(types=frozenset([kind]), literal=value)


def merge(values: list[Val]) -> Val:
    if not values:
        return literal(None)
    out = values[0]
    for value in values[1:]:
        same = out.literal is not _MISSING and value.literal is not _MISSING and out.literal == value.literal
        out = Val(refs=out.refs | value.refs, classes=out.classes | value.classes,
                  identities=out.identities | value.identities, origins=out.origins & value.origins,
                  types=out.types | value.types, unknown=out.unknown | value.unknown,
                  maps=out.maps + value.maps, literal=out.literal if same else _MISSING,
                  bound=out.bound + value.bound, pending=out.pending + value.pending,
                  future=out.future or value.future, apps=out.apps | value.apps)
    return out


@dataclass
class Function:
    key: str
    module: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner: str | None
    locals: set[str]
    globals: set[str]
    decorators: list[str] = field(default_factory=list)
    mode: str = "method"
    opaque: bool = False
    prefect: bool = False


@dataclass
class Class:
    key: str
    module: str
    node: ast.ClassDef
    bases: list[str]
    methods: dict[str, str]
    model: bool = False
    dynamic: bool = False


@dataclass
class State:
    env: dict[str, Val]
    attrs: dict[tuple[str, str], Val] = field(default_factory=dict)
    produced: set[tuple[str, str]] = field(default_factory=set)
    delivered: set[tuple[str, str]] = field(default_factory=set)
    returned: Val | None = None
    control: str = ""
    calls: dict[tuple[str, str, int, int], "State"] = field(default_factory=dict)
    uncertain: bool = False

    def copy(self) -> "State":
        # Copy mapping storage once per branch while retaining alias identity
        # within that branch. A sibling must never observe another branch's edit.
        memo: dict[int, Val] = {}
        containers: dict[int, dict[str, Val]] = {}
        def clone(value: Val) -> Val:
            if id(value) in memo:
                return memo[id(value)]
            copied_maps = []
            for mapping in value.maps:
                if id(mapping) not in containers:
                    copied = {}
                    containers[id(mapping)] = copied
                    copied.update({name: clone(item) for name, item in mapping.items()})
                copied_maps.append(containers[id(mapping)])
            result = replace(value, maps=tuple(copied_maps))
            memo[id(value)] = result
            return result
        return State({name: clone(value) for name, value in self.env.items()},
                     {key: clone(value) for key, value in self.attrs.items()},
                     self.produced.copy(), self.delivered.copy(),
                     clone(self.returned) if self.returned is not None else None, self.control,
                     {key: choice.copy() for key, choice in self.calls.items()}, self.uncertain)


class CallPaths(Exception):
    """A supported helper returned multiple paths; continue each separately."""

    def __init__(self, key: tuple[str, str, int, int], choices: list[State]):
        self.key = key
        self.choices = choices


class SourceError(ValueError):
    pass


class LimitError(ValueError):
    pass


class Program:
    def __init__(self, files: dict[str, str], config: Config):
        self.files = files
        self.config = config
        self.trees: dict[str, ast.Module] = {}
        self.paths: dict[str, str] = {}
        self.functions: dict[str, Function] = {}
        self.classes: dict[str, Class] = {}
        self.modules: dict[str, dict[str, Val]] = {}
        self.apps: dict[str, dict[str, Any]] = {}
        self.closures: dict[str, dict[str, Val]] = {}
        self.class_attrs: dict[tuple[str, str], Val] = {}
        self.tables: dict[tuple[str, int, str], Any] = {}
        count = 0
        for path, source in sorted(files.items()):
            if not path.endswith(".py"):
                continue
            module = self.module_name(path)
            if module in self.paths:
                raise SourceError(f"duplicate module identity {module}: {self.paths[module]} and {path}")
            try:
                tree = ast.parse(source, filename=path)
                table = symtable.symtable(source, path, "exec")
            except (SyntaxError, ValueError, RecursionError) as exc:
                raise SourceError(f"{path}: {exc}") from exc
            count += sum(1 for _ in ast.walk(tree))
            if count > config.max_nodes:
                raise LimitError(f"AST node limit {config.max_nodes} exceeded")
            self.trees[module] = tree
            self.paths[module] = path
            self.modules[module] = {}
            self.index_tables(module, table)
            self.index(module, tree.body)
        # Imports/definitions are indexed before interpreting declarations so
        # ordinary forward references and import cycles do not depend on order.
        for module, tree in self.trees.items():
            env = self.modules[module]
            for node in tree.body:
                self.declare(module, node, env)
        for module, tree in self.trees.items():
            for node in tree.body:
                self.module_statement(module, node)
        for cls in self.classes.values():
            cls.bases = [self.resolve_text(cls.module, base) for base in cls.bases]
            cls.model = "ext:pydantic.BaseModel" in cls.bases
        # Transitive model inheritance, independent of module order.
        for _ in self.classes:
            for cls in self.classes.values():
                cls.model = cls.model or any(base in self.classes and self.classes[base].model for base in cls.bases)

    def module_name(self, path: str) -> str:
        selected = path
        roots = sorted(self.config.source_roots, key=len, reverse=True)
        for root in roots:
            prefix = root.rstrip("/") + "/"
            if root not in (".", "") and path.startswith(prefix):
                selected = path[len(prefix):]
                break
        parts = selected[:-3].split("/")
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) or "__root__"

    def index_tables(self, module: str, table: Any) -> None:
        for child in table.get_children():
            self.tables[(module, child.get_lineno(), child.get_name())] = child
            self.index_tables(module, child)

    def index(self, module: str, nodes: list[ast.stmt], prefix: str = "", owner: str | None = None) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                key = module + ":" + name
                table = self.tables.get((module, node.lineno, node.name))
                local = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_local() or symbol.is_parameter()} if table else set()
                glob = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_global()} if table else set()
                if key in self.functions or key in self.classes:
                    raise SourceError(f"duplicate declaration identity {key}")
                self.functions[key] = Function(key, module, node, owner, local, glob)
                self.index(module, node.body, name + ".<locals>.")
            elif isinstance(node, ast.ClassDef):
                key = module + ":" + prefix + node.name
                if key in self.functions or key in self.classes:
                    raise SourceError(f"duplicate declaration identity {key}")
                self.classes[key] = Class(key, module, node, [ast.unparse(b) for b in node.bases], {}, dynamic=bool(node.keywords or node.decorator_list))
                self.index(module, node.body, prefix + node.name + ".", key)
                self.classes[key].methods = {f.node.name: f.key for f in self.functions.values() if f.owner == key}
                if set(self.classes[key].methods) & {"__getattribute__", "__getattr__", "__setattr__", "__get__"}:
                    self.classes[key].dynamic = True
                self.classes[key].model = False

    def resolve_text(self, module: str, text: str) -> str:
        bits = text.split(".")
        value = self.modules[module].get(bits[0])
        if value and value.refs:
            base = sorted(value.refs)[0]
            if base.startswith("mod:"):
                name = base[4:] + ("." + ".".join(bits[1:]) if len(bits) > 1 else "")
                if name in self.modules:
                    return "mod:" + name
                parent, _, item = name.rpartition(".")
                return parent + ":" + item if parent in self.modules else "ext:" + name
            return base + ("." + ".".join(bits[1:]) if len(bits) > 1 else "")
        if value and value.classes:
            return sorted(value.classes)[0] + ("." + ".".join(bits[1:]) if len(bits) > 1 else "")
        return "ext:" + text

    def imported(self, module: str, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        package = module if self.paths[module].endswith("/__init__.py") else module.rpartition(".")[0]
        parts = package.split(".") if package else []
        if node.level > len(parts) + 1:
            return "invalid_relative_import"
        head = parts[:len(parts) - node.level + 1]
        return ".".join(head + ([node.module] if node.module else []))

    def declare(self, module: str, node: ast.stmt, env: dict[str, Val]) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                env[name] = Val(refs=frozenset(["mod:" + (alias.name if alias.asname else name)]))
        elif isinstance(node, ast.ImportFrom):
            target = self.imported(module, node)
            for alias in node.names:
                if alias.name == "*":
                    env["*"] = unknown("wildcard import")
                    continue
                key = target + ":" + alias.name
                if key in self.functions:
                    val = Val(refs=frozenset([key]))
                elif key in self.classes:
                    val = Val(classes=frozenset([key]))
                elif target + "." + alias.name in self.modules:
                    val = Val(refs=frozenset(["mod:" + target + "." + alias.name]))
                elif target in self.modules:
                    val = Val(refs=frozenset(["import:" + key]))
                else:
                    val = Val(refs=frozenset(["ext:" + target + "." + alias.name]))
                env[alias.asname or alias.name] = val
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            key = module + ":" + node.name
            env[node.name] = Val(refs=frozenset([key]))
        elif isinstance(node, ast.ClassDef):
            env[node.name] = Val(classes=frozenset([module + ":" + node.name]))

    def static(self, module: str, node: ast.AST | None) -> Val:
        if node is None:
            return literal(None)
        if isinstance(node, ast.Constant):
            return literal(node.value)
        if isinstance(node, ast.Name):
            return self.modules[module].get(node.id, unknown(f"unknown global {node.id}"))
        if isinstance(node, ast.Attribute):
            ref = self.resolve_text(module, ast.unparse(node))
            return Val(refs=frozenset([ref]))
        if isinstance(node, ast.Dict):
            values = {}
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    return unknown("nonliteral mapping key")
                values[key.value] = self.static(module, value)
            return Val(maps=(values,), types=frozenset(["object"]))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            vals = [self.static(module, item) for item in node.elts]
            if all(v.literal is not _MISSING for v in vals):
                return literal([v.literal for v in vals])
            return replace(merge(vals), types=frozenset(["array"]))
        return unknown("dynamic module declaration")

    def module_statement(self, module: str, node: ast.stmt) -> None:
        env = self.modules[module]
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value_node = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(value_node, ast.Call) and self.resolve_text(module, ast.unparse(value_node.func)) == "ext:typer.Typer":
                name = targets[0].id if isinstance(targets[0], ast.Name) else f"line{node.lineno}"
                app = module + ":" + name
                callback = next((self.static(module, kw.value) for kw in value_node.keywords if kw.arg == "callback"), None)
                self.apps[app] = {"commands": [], "callback": callback, "children": []}
                value = Val(apps=frozenset([app]))
            else:
                value = self.static(module, value_node)
            for target in targets:
                if isinstance(target, ast.Name):
                    env[target.id] = value
                elif isinstance(target, ast.Attribute):
                    receiver = self.static(module, target.value)
                    for cls in receiver.classes:
                        self.class_attrs[(cls, target.attr)] = value
        elif isinstance(node, ast.If):
            condition = self.static(module, node.test)
            if condition.literal is not _MISSING:
                for item in node.body if condition.literal else node.orelse:
                    self.module_statement(module, item)
            elif "__name__" not in ast.unparse(node.test):
                for item in ast.walk(node):
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                        env[item.id] = unknown("conditional module binding")
                    if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Store):
                        for cls in self.static(module, item.value).classes:
                            self.class_attrs[(cls, item.attr)] = unknown("conditional class mutation")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.decorate(self.functions[module + ":" + node.name])
        elif isinstance(node, ast.ClassDef):
            cls = self.classes[module + ":" + node.name]
            local = env.copy()
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    key = cls.methods[item.name]
                    self.decorate(self.functions[key])
                    local[item.name] = Val(refs=frozenset([key]))
                    self.class_attrs.pop((cls.key, item.name), None)
                elif isinstance(item, ast.Assign):
                    value = local.get(item.value.id, unknown("unresolved class binding")) if isinstance(item.value, ast.Name) else self.static(module, item.value)
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            local[target.id] = value
                            self.class_attrs[(cls.key, target.id)] = value
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id in ("setattr", "delattr") and call.args:
                for cls in self.static(module, call.args[0]).classes:
                    self.classes[cls].dynamic = True
            if isinstance(call.func, ast.Attribute) and call.func.attr == "add_typer":
                parent = self.static(module, call.func.value)
                child = self.static(module, call.args[0]) if call.args else unknown("missing child application")
                for app in parent.apps:
                    if app in self.apps:
                        self.apps[app]["children"].extend(sorted(child.apps))

    def decorate(self, fn: Function) -> None:
        for node in fn.node.decorator_list:
            target = node.func if isinstance(node, ast.Call) else node
            text = ast.unparse(target)
            ref = self.resolve_text(fn.module, text)
            fn.decorators.append(ref)
            if text in ("staticmethod", "classmethod", "property") and text not in self.modules[fn.module]:
                fn.mode = text
            elif ref in ("ext:prefect.flow", "ext:prefect.task"):
                fn.prefect = True
            elif isinstance(target, ast.Attribute) and target.attr in ("command", "callback"):
                receiver = self.static(fn.module, target.value)
                if not receiver.apps:
                    fn.opaque = True
                for app in receiver.apps:
                    if app in self.apps:
                        value = Val(refs=frozenset([fn.key]))
                        if target.attr == "command":
                            self.apps[app]["commands"].append(value)
                        else:
                            self.apps[app]["callback"] = value
            else:
                fn.opaque = True

    def mro(self, cls: str, seen: set[str] | None = None) -> tuple[list[str], bool]:
        seen = set() if seen is None else seen
        if cls in seen or cls not in self.classes:
            return [cls], True
        info = self.classes[cls]
        seen.add(cls)
        opaque = info.dynamic
        sequences = []
        for base in info.bases:
            if base in ("ext:object", "ext:pydantic.BaseModel"):
                sequences.append([base])
            else:
                order, uncertain = self.mro(base, seen.copy())
                sequences.append(order)
                opaque |= uncertain
        sequences.append(info.bases.copy())
        order = [cls]
        while any(sequences):
            sequences = [seq for seq in sequences if seq]
            candidate = next((seq[0] for seq in sequences if not any(seq[0] in other[1:] for other in sequences)), None)
            if candidate is None:
                return order, True
            order.append(candidate)
            for seq in sequences:
                if seq[0] == candidate:
                    seq.pop(0)
        return order, opaque

    def method(self, cls: str, name: str, after: str | None = None) -> tuple[str | None, bool]:
        order, opaque = self.mro(cls)
        if after is not None:
            if after not in order:
                return None, True
            order = order[order.index(after) + 1:]
        for member in order:
            if member in self.classes and name in self.classes[member].methods:
                return self.classes[member].methods[name], opaque
        return None, opaque

    def model_fields(self, cls: str, seen: set[str] | None = None) -> tuple[dict[str, tuple[Val, bool, str]], set[str]]:
        seen = set() if seen is None else seen
        if cls in seen:
            return {}, {"cyclic model inheritance"}
        seen.add(cls)
        info = self.classes[cls]
        fields: dict[str, tuple[Val, bool, str]] = {}
        reasons: set[str] = set()
        if info.dynamic:
            reasons.add("dynamic model class decoration or metaclass")
        for base in reversed(info.bases):
            if base in self.classes:
                inherited, unsupported = self.model_fields(base, seen.copy())
                fields.update(inherited)
                reasons |= unsupported
            elif base not in ("ext:pydantic.BaseModel", "ext:BaseModel", "ext:object"):
                reasons.add(f"unknown model base {base}")
        for node in info.node.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                annotation = ast.unparse(node.annotation)
                if "ClassVar" in annotation or name.startswith("_"):
                    continue
                value = self.annotation(info.module, node.annotation)
                required = node.value is None or isinstance(node.value, ast.Constant) and node.value.value is Ellipsis
                alias = name
                if isinstance(node.value, ast.Call):
                    ref = self.resolve_text(info.module, ast.unparse(node.value.func))
                    if ref == "ext:pydantic.Field":
                        required = not node.value.args and not any(k.arg in ("default", "default_factory") for k in node.value.keywords)
                        if node.value.args:
                            required = isinstance(node.value.args[0], ast.Constant) and node.value.args[0].value is Ellipsis
                        for kw in node.value.keywords:
                            if kw.arg in ("alias", "serialization_alias"):
                                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                    alias = kw.value.value
                                else:
                                    reasons.add("nonliteral field alias")
                    else:
                        reasons.add("dynamic field declaration")
                fields[name] = (value, required, alias)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                text = ast.unparse(node)
                if "model_config" in text and any(x in text for x in ("alias_generator", "ignored_types")):
                    reasons.add("dynamic model configuration")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    if any(x in ast.unparse(deco) for x in ("serializer", "computed_field", "validator")):
                        reasons.add("custom model validator, serializer or computed field")
        return fields, reasons

    def model_declaration(self, cls: str, name: str) -> ast.AnnAssign | None:
        order, _ = self.mro(cls)
        for member in order:
            if member in self.classes:
                for node in self.classes[member].node.body:
                    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                        return node
        return None

    def model_default(self, cls: str, name: str) -> Val:
        node = self.model_declaration(cls, name)
        if node is None or node.value is None:
            return unknown(f"required model field {name} has no provided input")
        value = node.value
        module = self.classes[cls].module
        if isinstance(value, ast.Call) and self.resolve_text(module, ast.unparse(value.func)) == "ext:pydantic.Field":
            for keyword in value.keywords:
                if keyword.arg == "default_factory":
                    return unknown(f"dynamic default factory for field {name}")
                if keyword.arg == "default":
                    return self.static(module, keyword.value)
            if value.args:
                value = value.args[0]
            else:
                return unknown(f"required model field {name} has no provided input")
        if isinstance(value, ast.Constant) and value.value is Ellipsis:
            return unknown(f"required model field {name} has no provided input")
        return self.static(module, value)

    def annotation(self, module: str, node: ast.AST) -> Val:
        text = ast.unparse(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return merge([self.annotation(module, node.left), self.annotation(module, node.right)])
        if isinstance(node, ast.Subscript):
            name = ast.unparse(node.value).split(".")[-1]
            if name in ("Optional", "Union"):
                items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                vals = [self.annotation(module, item) for item in items]
                return merge(vals + ([literal(None)] if name == "Optional" else []))
            if name in ("dict", "Dict", "Mapping"):
                return Val(types=frozenset(["object"]))
            if name in ("list", "List", "tuple", "Tuple", "set", "Set", "Sequence"):
                return Val(types=frozenset(["array"]))
            if name == "Literal":
                items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                return merge([self.static(module, item) for item in items])
        kinds = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "dict": "object", "list": "array", "None": "null"}
        if text in kinds and text not in self.modules[module]:
            return Val(types=frozenset([kinds[text]]))
        resolved = self.resolve_text(module, text)
        if resolved in self.classes:
            # Declared interface is not an identified concrete receiver.
            return Val(classes=frozenset([resolved]), unknown=frozenset([f"annotation-only receiver {resolved}"]))
        return unknown(f"unsupported annotation {text}")


class Engine:
    def __init__(self, program: Program, contract: Any):
        self.p = program
        self.contract = contract
        self.edges: list[GraphEdge] = []
        self.reasons: set[str] = set()
        self.blocked: set[str] = set()
        self.visited: set[str] = set()
        self.stack: list[str] = []
        self.steps = 0
        self.instance_serial = 0
        self.current = contract.entry if hasattr(contract, "entry") else contract.source
        self.current_module = ""
        self.path_states: list[State] = []
        self.handoff_bad = False

    def tick(self) -> None:
        self.steps += 1
        if self.steps > self.p.config.max_steps:
            raise LimitError(f"analysis step limit {self.p.config.max_steps} exceeded")

    def loc(self, node: ast.AST, module: str | None = None) -> SourceLocation:
        return SourceLocation(path=self.p.paths.get(module or self.current_module, "<configuration>"), line=getattr(node, "lineno", 1), column=getattr(node, "col_offset", 0))

    def edge(self, target: str, node: ast.AST, kind: str = "call", detail: str = "") -> None:
        edge = GraphEdge(source=self.current, target=target, kind=kind, location=self.loc(node), detail=detail)
        if edge not in self.edges:
            self.edges.append(edge)

    def name(self, name: str, state: State) -> Val:
        if name in state.env:
            return state.env[name]
        value = self.p.modules[self.current_module].get(name)
        if value is not None:
            return self.resolve_import(value, set())
        if name in ("str", "int", "float", "bool", "dict", "list", "tuple", "set", "len", "print", "range", "super", "getattr", "setattr", "isinstance"):
            return Val(refs=frozenset(["builtin:" + name]))
        return unknown(f"unbound name {name}")

    def resolve_import(self, value: Val, seen: set[str]) -> Val:
        vals = []
        for ref in value.refs:
            if ref.startswith("import:") and ref not in seen:
                module, name = ref[7:].split(":", 1)
                vals.append(self.resolve_import(self.p.modules[module].get(name, unknown(f"unresolved import {module}:{name}")), seen | {ref}))
            else:
                vals.append(Val(refs=frozenset([ref])))
        return merge(vals) if vals else value

    def attribute(self, value: Val, name: str, node: ast.AST, state: State) -> Val:
        if value.future:
            if "future:map" in value.refs and name in ("result", "wait"):
                self.blocked.add("mapped futures form a collection, not a single future")
                state.uncertain = True
                return unknown("mapped futures form a collection, not a single future")
            if name == "result":
                return Val(bound=(("future:result", value),))
            if name in ("wait", "wait_for"):
                return Val(bound=(("future:wait", value),))
        vals: list[Val] = []
        for ref in value.refs:
            if ref.startswith("mod:"):
                module = ref[4:]
                if module in self.p.modules and name in self.p.modules[module]:
                    vals.append(self.resolve_import(self.p.modules[module][name], set()))
                elif module + "." + name in self.p.modules:
                    vals.append(Val(refs=frozenset(["mod:" + module + "." + name])))
                else:
                    vals.append(Val(refs=frozenset(["ext:" + module + "." + name])))
            elif ref.startswith("ext:"):
                vals.append(Val(refs=frozenset([ref + "." + name])))
            elif ref in self.p.functions and self.p.functions[ref].prefect and name in ("submit", "map", "with_options", "fn"):
                vals.append(Val(refs=frozenset([ref + "@" + name])))
        if value.apps and name == "add_typer":
            vals.append(Val(bound=(("typer:add_typer", value),)))
        stored = [state.attrs[(identity, name)] for identity in value.identities if (identity, name) in state.attrs]
        if stored and len(stored) == len(value.identities):
            return merge(stored)
        vals.extend(stored)
        for cls in value.classes:
            if cls not in self.p.classes:
                continue
            info = self.p.classes[cls]
            order, _ = self.p.mro(cls)
            overridden = next((self.p.class_attrs[(member, name)] for member in order if (member, name) in self.p.class_attrs), None)
            if overridden is not None:
                if value.identities and overridden.refs and not overridden.unknown:
                    vals.append(Val(bound=tuple((key, value) for key in overridden.refs if key in self.p.functions)))
                else:
                    vals.append(overridden)
                continue
            if info.model and name == "model_dump":
                vals.append(Val(bound=(("model:dump", value),)))
                continue
            if info.model:
                fields, reasons = self.p.model_fields(cls)
                if name in fields:
                    item = fields[name][0]
                    vals.append(replace(item, origins=frozenset((producer, name if not field else field + "." + name) for producer, field in value.origins), unknown=item.unknown | frozenset(reasons)))
                    continue
            after = next((ref[6:] for ref in value.refs if ref.startswith("super:")), None)
            key, opaque = self.p.method(cls, name, after)
            if key:
                fn = self.p.functions[key]
                receiver = value if fn.mode != "staticmethod" and value.identities else None
                if fn.mode == "classmethod":
                    receiver = Val(classes=frozenset([cls]))
                if fn.mode == "property" and value.identities:
                    vals.append(self.invoke(key, [], {}, state, node, receiver))
                elif fn.mode == "property":
                    vals.append(unknown("property accessed on a class"))
                elif receiver is not None:
                    vals.append(Val(bound=((key, receiver),), unknown=frozenset(["dynamic method dispatch"]) if opaque else value.unknown))
                else:
                    vals.append(Val(refs=frozenset([key]), unknown=frozenset(["dynamic method dispatch"]) if opaque else value.unknown))
            elif not value.identities:
                vals.append(unknown(f"unresolved member {cls}.{name}"))
        if value.maps and name in ("get", "items", "keys", "values"):
            vals.append(Val(bound=(("mapping:" + name, value),)))
        if not vals:
            return unknown(f"unsupported attribute {name}")
        result = merge(vals)
        if len(value.classes) > 1:
            result = replace(result, unknown=result.unknown | {"ambiguous receiver targets"})
        return result

    def expr(self, node: ast.AST | None, state: State, awaited: bool = False) -> Val:
        self.tick()
        if node is None:
            return literal(None)
        expression_key = ("expression:" + self.current, self.current_module, getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if expression_key in state.calls:
            choice = state.calls[expression_key].copy()
            state.attrs.update(choice.attrs)
            state.produced.update(choice.produced)
            state.delivered.update(choice.delivered)
            state.uncertain |= choice.uncertain
            return choice.env["$return"]
        if isinstance(node, ast.Constant):
            return literal(node.value)
        if isinstance(node, ast.Name):
            return self.name(node.id, state)
        if isinstance(node, ast.Attribute):
            return self.attribute(self.expr(node.value, state), node.attr, node, state)
        if isinstance(node, ast.Await):
            value = self.expr(node.value, state, True)
            if value.pending:
                return merge([self.invoke(key, list(args), dict(kws), state, node, receiver, kind="await") for key, args, kws, receiver in value.pending])
            return value
        if isinstance(node, ast.Call):
            # super() is resolved only inside an identified ordinary class.
            if isinstance(node.func, ast.Name) and node.func.id == "super" and node.func.id not in state.env and node.func.id not in self.p.modules[self.current_module]:
                fn = self.p.functions.get(self.current)
                if fn and fn.owner in self.p.classes:
                    args = fn.node.args.posonlyargs + fn.node.args.args
                    receiver = state.env.get(args[0].arg, unknown("unbound super receiver")) if args else unknown("super without receiver")
                    if node.args:
                        return unknown("explicit super arguments are unsupported")
                    return replace(receiver, refs=frozenset(["super:" + fn.owner]))
                return unknown("unresolved super()")
            function = self.expr(node.func, state)
            args = [self.expr(arg, state) for arg in node.args]
            kwargs: dict[str, Val] = {}
            for kw in node.keywords:
                if kw.arg is None:
                    expansion = self.expr(kw.value, state)
                    if len(expansion.maps) == 1:
                        kwargs.update(expansion.maps[0])
                    else:
                        self.reasons.add("dynamic keyword expansion")
                else:
                    kwargs[kw.arg] = self.expr(kw.value, state)
            return self.call(function, args, kwargs, state, node, awaited)
        if isinstance(node, ast.Dict):
            mapping: dict[str, Val] = {}
            reasons: set[str] = set()
            for key, item in zip(node.keys, node.values):
                val = self.expr(item, state)
                if key is None:
                    if len(val.maps) == 1:
                        mapping.update(val.maps[0])
                    else:
                        reasons.add("dynamic mapping expansion")
                else:
                    k = self.expr(key, state)
                    if isinstance(k.literal, str):
                        mapping[k.literal] = val
                    else:
                        reasons.add("nonliteral mapping key")
            return Val(maps=(mapping,), types=frozenset(["object"]), unknown=frozenset(reasons))
        if isinstance(node, ast.Subscript):
            value = self.expr(node.value, state)
            key = self.expr(node.slice, state)
            if isinstance(key.literal, str):
                vals = [mapping.get(key.literal, unknown(f"missing field {key.literal}")) for mapping in value.maps]
                origins = frozenset((producer, key.literal if not field else field + "." + key.literal) for producer, field in value.origins)
                if vals:
                    result = merge(vals)
                    return result
                if origins:
                    self.blocked.add("selected producer field is not statically identified")
                    state.uncertain = True
                    return unknown("selected producer field is not statically identified")
            if isinstance(key.literal, int) and isinstance(value.literal, (list, tuple)) and -len(value.literal) <= key.literal < len(value.literal):
                return literal(value.literal[key.literal])
            return unknown("dynamic subscript or unknown container")
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            vals = [self.expr(item, state) for item in node.elts]
            if all(v.literal is not _MISSING for v in vals):
                return replace(literal([v.literal for v in vals]), origins=frozenset().union(*(v.origins for v in vals)))
            return Val(types=frozenset(["array"]), maps=({str(i): v for i, v in enumerate(vals)},), origins=frozenset().union(*(v.origins for v in vals)), unknown=frozenset().union(*(v.unknown for v in vals)))
        if isinstance(node, ast.IfExp):
            condition = self.expr(node.test, state)
            if condition.literal is not _MISSING:
                return self.expr(node.body if condition.literal else node.orelse, state)
            branches = [state.copy(), state.copy()]
            values = [self.expr(node.body, branches[0]), self.expr(node.orelse, branches[1])]
            ambiguous_dispatch = any(value.refs or value.bound or value.classes or value.apps for value in values)
            if getattr(self.contract, "kind", None) == "handoff" and not ambiguous_dispatch:
                raise CallPaths(expression_key, [State({"$return": value}, branch.attrs, branch.produced, branch.delivered, uncertain=branch.uncertain) for value, branch in zip(values, branches)])
            if branches[0].delivered != branches[1].delivered:
                self.handoff_bad = True
            for branch in branches:
                state.produced.update(branch.produced)
                state.delivered.update(branch.delivered)
            return merge(values)
        if isinstance(node, ast.BoolOp):
            values = []
            conditional = False
            delivered = state.delivered.copy()
            for expression in node.values:
                value = self.expr(expression, state)
                values.append(value)
                if value.literal is not _MISSING:
                    if isinstance(node.op, ast.And) and not value.literal or isinstance(node.op, ast.Or) and value.literal:
                        break
                else:
                    conditional = True
            if conditional and delivered != state.delivered:
                self.handoff_bad = True
            return merge(values)
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
            # No evaluation of lazy generator bodies, and no invented bindings
            # for comprehensions. Unsupported lazy dispatch remains explicit.
            if not isinstance(node, ast.GeneratorExp):
                self.reasons.add("comprehension iteration and binding are unresolved")
                targets = [getattr(self.contract, "target", ""), getattr(self.contract, "producer", ""), getattr(self.contract, "consumer", "") or ""]
                referenced = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)} | {child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)}
                if any(target and target.split(":")[-1].split(".")[-1] in referenced for target in targets):
                    self.blocked.add("comprehension iteration and binding are unresolved")
                    state.uncertain = True
            return unknown("lazy generator expression" if isinstance(node, ast.GeneratorExp) else "unsupported comprehension")
        if isinstance(node, ast.NamedExpr):
            value = self.expr(node.value, state)
            self.assign(node.target, value, state)
            return value
        if isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.JoinedStr)):
            vals = [self.expr(child, state) for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)]
            types = frozenset().union(*(v.types for v in vals))
            # Transformations preserve dependency evidence but not a literal result.
            if isinstance(node, (ast.Compare, ast.BoolOp)):
                types = frozenset(["boolean"])
            elif isinstance(node, ast.JoinedStr):
                types = frozenset(["string"])
            return Val(types=types, origins=frozenset().union(*(v.origins for v in vals)), unknown=frozenset().union(*(v.unknown for v in vals)))
        if isinstance(node, ast.FormattedValue):
            return self.expr(node.value, state)
        if isinstance(node, ast.Lambda):
            return unknown("lambda dispatch is unsupported")
        # Do not drop supported child calls, but retain uncertainty about the value.
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.expr(child, state)
        return unknown(f"unsupported expression {type(node).__name__}")

    def call(self, value: Val, args: list[Val], kwargs: dict[str, Val], state: State, node: ast.AST, awaited: bool = False) -> Val:
        outputs: list[Val] = []
        if len(value.refs) > 1 or len(value.bound) > 1 or len(value.classes) > 1 or len(value.apps) > 1:
            reason = "ambiguous callable targets; candidate bodies are not proven"
            self.reasons.add(reason)
            self.blocked.add(reason)
            state.uncertain = True
            for candidate in sorted(value.refs | frozenset(key for key, _ in value.bound)):
                if candidate in self.p.functions:
                    self.edge(candidate, node, "candidate", reason)
            return unknown(reason)
        if value.unknown:
            self.reasons.update(value.unknown)
            text = ast.unparse(node.func) if isinstance(node, ast.Call) else ""
            selected_targets = [getattr(self.contract, "target", ""), getattr(self.contract, "producer", ""), getattr(self.contract, "consumer", "") or ""]
            relevant_name = any(target and target.split(":")[-1].split(".")[-1] in text.split(".") for target in selected_targets)
            relevant_value = any(v.origins for v in args + list(kwargs.values()))
            if relevant_name or relevant_value or any("receiver" in reason or "dispatch" in reason for reason in value.unknown):
                self.blocked.update(value.unknown)
                state.uncertain = True
            # An interface/candidate is not enough evidence to follow its body.
            if (value.bound or value.refs or value.classes) and not (value.bound and all(key == "model:dump" for key, _ in value.bound)):
                return replace(unknown("receiver or callable identity is not proven"), unknown=value.unknown | {"receiver or callable identity is not proven"})
        for app in value.apps:
            self.run_app(app, state, node)
            outputs.append(literal(None))
        for cls in value.classes:
            if cls not in self.p.classes:
                continue
            info = self.p.classes[cls]
            self.instance_serial += 1
            identity = f"{self.current}@{self.loc(node).line}:{self.loc(node).column}:{cls}#{self.instance_serial}"
            instance = Val(classes=frozenset([cls]), identities=frozenset([identity]), types=frozenset(["object"]))
            if info.dynamic or self.p.method(cls, "__new__")[0] is not None:
                outputs.append(unknown(f"custom constructor or metaclass {cls}"))
            elif info.model:
                fields, reasons = self.p.model_fields(cls)
                for name, (expected, _, _) in fields.items():
                    supplied = kwargs.get(name)
                    declaration = self.p.model_declaration(cls, name)
                    if supplied is None and declaration and isinstance(declaration.value, ast.Call):
                        for keyword in declaration.value.keywords:
                            if keyword.arg in ("alias", "validation_alias"):
                                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                                    supplied = kwargs.get(keyword.value.value)
                                else:
                                    reasons.add("dynamic validation alias")
                    state.attrs[(identity, name)] = supplied if supplied is not None else self.p.model_default(cls, name)
                outputs.append(replace(instance, unknown=frozenset(reasons)))
            else:
                constructor, opaque = self.p.method(cls, "__init__")
                if constructor:
                    self.invoke(constructor, args, kwargs, state, node, instance)
                elif opaque and any(base != "ext:object" for base in info.bases):
                    self.reasons.add(f"unknown constructor inheritance {cls}")
                outputs.append(instance)
        for key, receiver in value.bound:
            if key == "future:result":
                outputs.append(replace(receiver, future=False, bound=(), refs=frozenset()))
            elif key == "future:wait":
                outputs.append(literal(None))
            elif key == "typer:add_typer":
                for parent in receiver.apps:
                    if args:
                        self.p.apps[parent]["children"].extend(sorted(args[0].apps))
                outputs.append(literal(None))
            elif key.startswith("mapping:"):
                if key == "mapping:get" and args and isinstance(args[0].literal, str):
                    name = args[0].literal
                    got = merge([mapping.get(name, args[1] if len(args) > 1 else literal(None)) for mapping in receiver.maps])
                    outputs.append(got)
                elif key == "mapping:keys":
                    outputs.append(literal(sorted(set().union(*(m.keys() for m in receiver.maps)))))
                else:
                    outputs.append(unknown("mapping iteration does not prove payload delivery"))
            elif key == "model:dump":
                outputs.append(self.model_dump(receiver, kwargs, state))
            else:
                fn = self.p.functions[key]
                if isinstance(fn.node, ast.AsyncFunctionDef) and not awaited and not fn.prefect:
                    outputs.append(Val(pending=((key, tuple(args), tuple(kwargs.items()), receiver),)))
                else:
                    outputs.append(self.invoke(key, args, kwargs, state, node, receiver, "await" if awaited else "call"))
        for ref in value.refs:
            target, _, modifier = ref.partition("@")
            if target in self.p.functions:
                fn = self.p.functions[target]
                if modifier == "with_options":
                    outputs.append(Val(refs=frozenset([target])))
                elif modifier in ("submit", "map"):
                    scheduled = self.invoke(target, args, {k: v for k, v in kwargs.items() if k != "wait_for"}, state, node, kind="schedule")
                    outputs.append(replace(scheduled, future=True, refs=frozenset(["future:map"]) if modifier == "map" else scheduled.refs))
                elif isinstance(fn.node, ast.AsyncFunctionDef) and not awaited:
                    outputs.append(Val(pending=((target, tuple(args), tuple(kwargs.items()), None),)))
                else:
                    outputs.append(self.invoke(target, args, kwargs, state, node, kind="await" if awaited else "call", bypass=modifier == "fn"))
            elif ref.startswith("builtin:"):
                name = ref[8:]
                types = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "len": "integer", "dict": "object", "list": "array", "tuple": "array", "set": "array"}
                if name in types:
                    origins = frozenset().union(*(v.origins for v in args), *(v.origins for v in kwargs.values()))
                    if name == "dict" and kwargs:
                        outputs.append(Val(maps=(kwargs,), types=frozenset(["object"]), origins=origins))
                    else:
                        outputs.append(Val(types=frozenset([types[name]]), origins=origins))
                elif name in ("getattr", "setattr"):
                    self.reasons.add("reflection affects reachable code")
                    self.blocked.add("reflection affects reachable code")
                    state.uncertain = True
                    outputs.append(unknown("reflection affects reachable code"))
                else:
                    outputs.append(literal(None))
            elif ref in ("ext:asyncio.run", "ext:asyncio.create_task", "ext:asyncio.ensure_future") and args:
                for key, pos, kws, receiver in args[0].pending:
                    outputs.append(self.invoke(key, list(pos), dict(kws), state, node, receiver, "schedule"))
            else:
                reason = f"opaque external call {ref.removeprefix('ext:')}"
                self.reasons.add(reason)
                outputs.append(unknown(reason))
        if not outputs:
            reason = "; ".join(sorted(value.unknown)) or "unsupported callable dispatch"
            self.reasons.add(reason)
            return unknown(reason)
        return merge(outputs)

    def model_dump(self, receiver: Val, kwargs: dict[str, Val], state: State) -> Val:
        results = []
        for cls in receiver.classes:
            fields, reasons = self.p.model_fields(cls)
            for option in ("include", "exclude"):
                if option in kwargs and not isinstance(kwargs[option].literal, (list, tuple, set, dict)):
                    reasons.add(f"conditional or dynamic {option} projection")
            for option in ("exclude_unset", "exclude_defaults", "exclude_none"):
                if option in kwargs and kwargs[option].literal is not False:
                    reasons.add(f"conditional {option} projection")
            alias = kwargs.get("by_alias", literal(False))
            if not isinstance(alias.literal, bool):
                reasons.add("conditional alias projection")
            mapping = {}
            include = kwargs.get("include", literal(list(fields))).literal
            exclude = kwargs.get("exclude", literal([])).literal
            for name, (val, _, serialized) in fields.items():
                declaration = self.p.model_declaration(cls, name)
                omitted = False
                if declaration and isinstance(declaration.value, ast.Call):
                    for keyword in declaration.value.keywords:
                        if keyword.arg == "exclude":
                            exclusion = self.p.static(self.p.classes[cls].module, keyword.value)
                            if exclusion.literal is True:
                                omitted = True
                            elif exclusion.literal not in (False, None):
                                reasons.add(f"dynamic field exclusion for {name}")
                        elif keyword.arg == "exclude_if":
                            reasons.add(f"conditional field exclusion for {name}")
                if omitted:
                    continue
                if isinstance(include, (list, tuple, set, dict)) and name not in include:
                    continue
                if isinstance(exclude, (list, tuple, set, dict)) and name in exclude:
                    continue
                vals = [state.attrs.get((identity, name), val) for identity in receiver.identities]
                mapping[serialized if alias.literal is True else name] = merge(vals) if vals else val
            results.append(Val(maps=(mapping,), types=frozenset(["object"]), unknown=frozenset(reasons)))
        return merge(results)

    def bind(self, fn: Function, args: list[Val], kwargs: dict[str, Val], receiver: Val | None) -> dict[str, Val]:
        env = {name: unknown(f"unbound local {name}") for name in fn.locals}
        env.update(self.p.closures.get(fn.key, {}))
        positional = fn.node.args.posonlyargs + fn.node.args.args
        actual = ([receiver] if receiver is not None else []) + args
        defaults = [None] * (len(positional) - len(fn.node.args.defaults)) + fn.node.args.defaults
        for i, param in enumerate(positional):
            if i < len(actual):
                env[param.arg] = actual[i]
            elif param.arg in kwargs:
                env[param.arg] = kwargs[param.arg]
            elif defaults[i] is not None:
                env[param.arg] = self.p.static(fn.module, defaults[i])
            elif param.annotation is not None:
                env[param.arg] = self.p.annotation(fn.module, param.annotation)
            else:
                env[param.arg] = unknown(f"unknown argument {param.arg}")
        for param, default in zip(fn.node.args.kwonlyargs, fn.node.args.kw_defaults):
            env[param.arg] = kwargs.get(param.arg, self.p.static(fn.module, default) if default is not None else self.p.annotation(fn.module, param.annotation) if param.annotation else unknown(f"unknown argument {param.arg}"))
        if fn.node.args.vararg:
            env[fn.node.args.vararg.arg] = merge(actual[len(positional):])
        if fn.node.args.kwarg:
            env[fn.node.args.kwarg.arg] = Val(maps=(kwargs,), types=frozenset(["object"]))
        return env

    def invoke(self, key: str, args: list[Val], kwargs: dict[str, Val], state: State, node: ast.AST, receiver: Val | None = None, kind: str = "call", bypass: bool = False) -> Val:
        self.tick()
        call_key = (key, self.current_module, getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if call_key in state.calls:
            choice = state.calls[call_key].copy()
            state.attrs.update(choice.attrs)
            state.produced.update(choice.produced)
            state.delivered.update(choice.delivered)
            state.uncertain |= choice.uncertain
            return choice.env["$return"]
        fn = self.p.functions[key]
        if fn.opaque and not bypass:
            self.reasons.add(f"opaque decorator on {key}")
            self.blocked.add(f"opaque decorator on {key}")
            state.uncertain = True
            return unknown(f"opaque decorator on {key}")
        # A generator call creates an iterator; it does not enter the body.
        def generator_body(nodes: list[ast.stmt]) -> bool:
            pending = list(nodes)
            while pending:
                item = pending.pop()
                if isinstance(item, (ast.Yield, ast.YieldFrom)):
                    return True
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    pending.extend(ast.iter_child_nodes(item))
            return False
        if generator_body(fn.node.body):
            self.reasons.add(f"generator body requires iteration: {key}")
            self.blocked.add(f"generator body requires iteration: {key}")
            state.uncertain = True
            return unknown(f"unconsumed generator {key}")
        self.edge(key, node, kind, "Prefect adapter structural invocation" if fn.prefect and not bypass else "explicit decorator bypass" if bypass else "")
        self.visited.add(key)
        if key in self.stack:
            self.reasons.add(f"recursive return flow at {key}")
            return unknown(f"recursive return flow at {key}")
        if len(self.stack) >= 64:
            raise LimitError("call depth limit 64 exceeded")
        if fn.prefect and not bypass:
            args = [replace(v, future=False) for v in args]
            kwargs = {name: replace(v, future=False) if name != "wait_for" else v for name, v in kwargs.items()}
        env = self.bind(fn, args, kwargs, receiver)
        contract = self.contract
        if getattr(contract, "kind", None) == "handoff" and key == contract.consumer:
            params = fn.node.args.posonlyargs + fn.node.args.args
            if isinstance(contract.argument, int):
                val = args[contract.argument] if contract.argument < len(args) else unknown("consumer argument absent")
            else:
                val = env.get(contract.argument, unknown("consumer argument absent"))
            if not val.future:
                for producer, selected in val.origins:
                    if producer.startswith(contract.producer + "@") and (contract.field is None or selected == contract.field):
                        state.delivered.add((producer, selected))
                        self.edge(key, node, "handoff", f"producer result reaches argument {contract.argument}")
        previous = self.current, self.current_module
        self.current, self.current_module = key, fn.module
        self.stack.append(key)
        inner = State(env, state.attrs.copy(), state.produced.copy(), state.delivered.copy(), uncertain=state.uncertain)
        endings = self.block(fn.node.body, [inner])
        self.stack.pop()
        self.current, self.current_module = previous
        if not endings:
            endings = [inner]
        values = [ending.returned if ending.returned is not None else literal(None) for ending in endings]
        if key == getattr(contract, "producer", None):
            identity = key + f"@{self.loc(node, previous[1]).path}:{getattr(node, 'lineno', 1)}:{getattr(node, 'col_offset', 0)}"
            def tag_fields(value: Val, prefix: str) -> Val:
                for mapping in value.maps:
                    for field_name, field_value in list(mapping.items()):
                        selected = field_name if not prefix else prefix + "." + field_name
                        mapping[field_name] = tag_fields(field_value, selected)
                return replace(value, origins=value.origins | {(identity, prefix)})
            values = [tag_fields(value, "") for value in values]
            for value, ending in zip(values, endings):
                ending.produced.add((identity, ""))
                for instance in value.identities:
                    for (owner, name), field_value in list(ending.attrs.items()):
                        if owner == instance:
                            ending.attrs[(owner, name)] = tag_fields(field_value, name)
        if len(endings) > 1 and getattr(contract, "kind", None) == "handoff" and self.stack:
            if fn.node.name == "__init__":
                # Constructor receivers cannot be replayed without preserving
                # their allocation identity. Retain uncertainty rather than a
                # branch-mixed delivery proof.
                self.blocked.add("branch-dependent constructor effects")
                self.reasons.add("branch-dependent constructor effects")
                state.uncertain = True
            else:
                raise CallPaths(call_key, [State({"$return": value}, ending.attrs, ending.produced, ending.delivered, uncertain=ending.uncertain) for value, ending in zip(values, endings)])
        # Keep branch-specific mapping alternatives, never union field keys.
        output = merge(values)
        for ending in endings:
            state.produced.update(ending.produced)
            state.delivered.update(ending.delivered)
            state.uncertain |= ending.uncertain
        for attr in set().union(*(ending.attrs.keys() for ending in endings)):
            state.attrs[attr] = merge([ending.attrs.get(attr, unknown("branch-dependent receiver field")) for ending in endings])
        if len(self.stack) == 0:
            self.path_states = endings
        return output

    def assign(self, target: ast.AST, value: Val, state: State) -> None:
        if isinstance(target, ast.Name):
            state.env[target.id] = value
        elif isinstance(target, ast.Attribute):
            receiver = self.expr(target.value, state)
            if receiver.identities:
                for identity in receiver.identities:
                    state.attrs[(identity, target.attr)] = value
            else:
                self.reasons.add("monkeypatching or unresolved attribute assignment")
                self.blocked.add("monkeypatching or unresolved attribute assignment")
                state.uncertain = True
        elif isinstance(target, (ast.Tuple, ast.List)):
            for i, item in enumerate(target.elts):
                values = [mapping.get(str(i), unknown("unknown unpacking element")) for mapping in value.maps]
                if isinstance(value.literal, (list, tuple)) and i < len(value.literal):
                    part = literal(value.literal[i])
                else:
                    part = merge(values) if values else unknown("unknown unpacking element")
                self.assign(item, part, state)
        elif isinstance(target, ast.Subscript):
            container = self.expr(target.value, state)
            key = self.expr(target.slice, state)
            if isinstance(key.literal, str) and container.maps:
                for mapping in container.maps:
                    mapping[key.literal] = value
            else:
                self.reasons.add("dynamic container assignment")

    def block(self, nodes: list[ast.stmt], states: list[State]) -> list[State]:
        for node in nodes:
            next_states = []
            for state in states:
                if state.returned is not None or state.control:
                    next_states.append(state)
                    continue
                if getattr(self.contract, "kind", None) != "handoff":
                    next_states.extend(self.statement(node, state))
                    continue
                pending = [state]
                while pending:
                    before = pending.pop()
                    candidate = before.copy()
                    try:
                        results = self.statement(node, candidate)
                    except CallPaths as split:
                        for choice in split.choices:
                            alternative = before.copy()
                            alternative.calls[split.key] = choice
                            pending.append(alternative)
                    else:
                        for result in results:
                            result.calls.clear()
                        next_states.extend(results)
                    if len(pending) + len(next_states) > 128:
                        raise LimitError("branch state limit 128 exceeded")
            states = next_states
            if len(states) > 128:
                raise LimitError("branch state limit 128 exceeded")
        return states

    def statement(self, node: ast.stmt, state: State) -> list[State]:
        self.tick()
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = self.expr(node.value, state)
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                self.assign(target, value, state)
        elif isinstance(node, ast.AugAssign):
            before = self.expr(node.target, state)
            after = self.expr(node.value, state)
            self.assign(node.target, merge([before, after]), state)
        elif isinstance(node, ast.Expr):
            value = self.expr(node.value, state)
            if value.pending:
                self.reasons.add("unawaited coroutine does not reach its body")
        elif isinstance(node, ast.Return):
            state.returned = self.expr(node.value, state)
            contract = self.contract
            if getattr(contract, "kind", None) == "handoff" and contract.returned_field and self.current == contract.entry:
                for mapping in state.returned.maps:
                    val = mapping.get(contract.returned_field, literal(None))
                    for producer, selected in val.origins:
                        if producer.startswith(contract.producer + "@") and (contract.field is None or selected == contract.field):
                            state.delivered.add((producer, selected))
        elif isinstance(node, ast.If):
            value = self.expr(node.test, state)
            # Only constants can remove an unreachable branch. Both symbolic
            # branches remain independent, including return shapes.
            if value.literal is not _MISSING:
                return self.block(node.body if value.literal else node.orelse, [state])
            return self.block(node.body, [state.copy()]) + self.block(node.orelse, [state.copy()])
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            if isinstance(node, (ast.For, ast.AsyncFor)):
                iterable = self.expr(node.iter, state)
                copied = state.copy()
                self.assign(node.target, unknown("loop element"), copied)
            else:
                self.expr(node.test, state)
                copied = state.copy()
            self.reasons.add("loop iteration count and loop-carried values are unresolved")
            results = self.block(node.body, [copied])
            for ending in results:
                ending.control = ""
            return self.block(node.orelse, results) + self.block(node.orelse, [state.copy()])
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                value = self.expr(item.context_expr, state)
                if item.optional_vars:
                    self.assign(item.optional_vars, value, state)
            return self.block(node.body, [state])
        elif isinstance(node, ast.Try):
            results = self.block(node.body, [state.copy()])
            # Handler paths depend on exceptions, not static successful calls.
            if node.handlers:
                self.reasons.add("exception dispatch is unresolved")
            for handler in node.handlers:
                caught = state.copy()
                if handler.name:
                    caught.env[handler.name] = unknown("exception value")
                results += self.block(handler.body, [caught])
            results = self.block(node.orelse, results)
            for ending in results:
                returned = ending.returned
                ending.returned = None
                self.block(node.finalbody, [ending])
                if ending.returned is None:
                    ending.returned = returned
            return results
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            key = self.current + ".<locals>." + node.name
            if key in self.p.functions:
                self.p.closures[key] = state.env.copy()
                state.env[node.name] = Val(refs=frozenset([key]))
                self.p.decorate(self.p.functions[key])
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.p.declare(self.current_module, node, state.env)
        elif isinstance(node, ast.Assert):
            self.expr(node.test, state)
        elif isinstance(node, ast.Raise):
            if node.exc:
                self.expr(node.exc, state)
            state.returned = unknown("raised path has no produced payload")
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    state.env[target.id] = unknown("deleted binding")
        elif isinstance(node, (ast.Break, ast.Continue)):
            state.control = type(node).__name__
        elif isinstance(node, (ast.Pass, ast.Global, ast.Nonlocal)):
            pass
        else:
            self.reasons.add(f"unsupported statement {type(node).__name__}")
            self.blocked.add(f"unsupported statement {type(node).__name__}")
            state.uncertain = True
        return [state]

    def run_app(self, app: str, state: State, node: ast.AST, seen: set[str] | None = None) -> None:
        seen = set() if seen is None else seen
        if app in seen:
            self.reasons.add("cyclic Typer application registration")
            return
        seen.add(app)
        info = self.p.apps.get(app)
        if info is None:
            self.reasons.add("unknown Typer application")
            return
        previous = self.current
        self.edge(app, node, "application", "Typer dispatch, selected command is not observed")
        self.current = app
        for value in ([info["callback"]] if info["callback"] else []) + info["commands"]:
            for key in value.refs:
                self.invoke(key, [], {}, state, node, kind="registration")
        for child in info["children"]:
            self.run_app(child, state, node, seen.copy())
        self.current = previous

    def run(self, entry: str) -> tuple[Val, State]:
        module, sep, name = entry.partition(":")
        if not sep or module not in self.p.modules:
            raise SourceError(f"selected entry {entry} is not in the inspected source scope")
        self.current_module = module
        self.current = entry
        state = State({})
        if entry in self.p.functions:
            # Do not emit a self-edge for a configured entry.
            self.current = "<entry>"
            value = self.invoke(entry, [], {}, state, self.p.functions[entry].node, kind="entry")
            self.current = entry
        else:
            value = self.p.modules[module].get(name)
            if value is None or not value.apps:
                raise SourceError(f"selected entry {entry} is not a function or Typer application")
            self.run_app(next(iter(value.apps)), state, self.p.trees[module])
            value = literal(None)
        return value, state

    def witness(self, entry: str, target: str) -> list[GraphEdge]:
        adjacency: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.source, []).append(edge)
        queue = deque([(entry, [])])
        seen = set()
        while queue:
            node, path = queue.popleft()
            if node == target:
                return path
            if node in seen:
                continue
            seen.add(node)
            queue.extend((edge.target, path + [edge]) for edge in adjacency.get(node, []))
        return []


def selected(path: str, config: Config) -> bool:
    roots = any(root == "." or path == root or path.startswith(root.rstrip("/") + "/") for root in config.source_roots)
    return roots and not any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(PurePosixPath(path).name, pattern) for pattern in config.excludes)


def status_of(statuses: list[str]) -> str:
    return next((status for status in ("ERROR", "FAIL", "UNRESOLVED") if status in statuses), "PASS")


def base_report(files: dict[str, str], config: Any, **kwargs: Any) -> AnalysisReport:
    dumped = config.model_dump(mode="json") if isinstance(config, Config) else config
    try:
        config_hash = digest(dumped)
    except (TypeError, ValueError):
        config_hash = hashlib.sha256(b"invalid configuration").hexdigest()
    return AnalysisReport(checker_version=VERSION, parser_version="Python " + platform.python_version() + " ast/symtable",
                          source_hash=digest(files), config_hash=config_hash, scope=sorted(files), **kwargs)


def shape_result(program: Program, contract: Any) -> tuple[ContractResult, list[GraphEdge]]:
    engine = Engine(program, contract)
    failures: list[Finding] = []
    reasons: set[str] = set()
    shapes: list[dict[str, Val]] = []
    required_fields: dict[str, bool] | None = None
    location: SourceLocation | None = None
    if contract.mode == "artifact":
        if contract.source not in program.files:
            raise SourceError(f"selected artifact {contract.source} is missing")
        try:
            artifact = json.loads(program.files[contract.source], parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-JSON number {value}")))
        except (ValueError, RecursionError) as exc:
            raise SourceError(f"invalid JSON artifact {contract.source}: {exc}") from exc
        location = SourceLocation(path=contract.source, line=1)
        if not isinstance(artifact, dict):
            failures.append(Finding(code="shape-not-object", message="selected JSON artifact is not an object", location=location))
        else:
            shapes = [{name: literal(value) for name, value in artifact.items()}]
    else:
        module, sep, name = contract.source.partition(":")
        if not sep or module not in program.modules:
            raise SourceError(f"selected shape source {contract.source} is outside the inspected scope")
        engine.current_module = module
        if contract.mode == "model":
            if contract.source not in program.classes or not program.classes[contract.source].model:
                raise SourceError(f"selected shape source {contract.source} is not a declared Pydantic model")
            fields, reasons = program.model_fields(contract.source)
            shapes = [{name: val for name, (val, _, _) in fields.items()}]
            required_fields = {name: required for name, (_, required, _) in fields.items()}
            location = engine.loc(program.classes[contract.source].node)
        else:
            if contract.source in program.functions:
                value, _ = engine.run(contract.source)
                location = engine.loc(program.functions[contract.source].node, module)
            else:
                value = program.modules[module].get(name, unknown(f"unresolved public declaration {contract.source}"))
                location = SourceLocation(path=program.paths[module], line=1)
            reasons.update(value.unknown)
            if contract.mode == "exports":
                if isinstance(value.literal, (list, tuple, set)) and all(isinstance(item, str) for item in value.literal):
                    shapes = [{item: literal(item) for item in value.literal}]
                else:
                    reasons.add("public export set is not a literal string collection")
            else:
                shapes = list(value.maps)
                if not shapes:
                    if value.unknown:
                        reasons.update(value.unknown)
                    elif value.types and "object" not in value.types:
                        failures.append(Finding(code="shape-not-object", message="an analyzed return path does not produce a mapping", location=location))
                    else:
                        reasons.add("produced mapping keys are unresolved")
                # Any non-object alternative is a distinct path, never hidden by
                # another branch's complete mapping.
                if value.types - {"object"}:
                    failures.append(Finding(code="shape-return-path", message="an analyzed return path does not produce a mapping", location=location))
    for branch, mapping in enumerate(shapes, 1):
        missing = [name for name, spec in contract.fields.items() if spec.required and name not in mapping]
        extra = sorted(set(mapping) - set(contract.fields)) if not contract.allow_extra else []
        if missing:
            failures.append(Finding(code="shape-missing", message=f"shape {branch} is missing required fields: {', '.join(sorted(missing))}", location=location))
        if extra:
            failures.append(Finding(code="shape-extra", message=f"shape {branch} has undeclared fields: {', '.join(extra)}", location=location))
        for name, spec in contract.fields.items():
            if name not in mapping:
                continue
            val = mapping[name]
            if required_fields is not None and required_fields[name] != spec.required:
                failures.append(Finding(code="shape-required", message=f"field {name} required/default status differs from the contract", location=location))
            allowed = {spec.type}
            if spec.type == "number":
                allowed.add("integer")
            if spec.nullable:
                allowed.add("null")
            incompatible = val.types - allowed if spec.type != "any" else set()
            if not spec.nullable and "null" in val.types and spec.type != "null":
                incompatible |= {"null"}
            if incompatible:
                failures.append(Finding(code="shape-type", message=f"field {name} can have incompatible JSON types: {', '.join(sorted(incompatible))}", location=location))
            if val.unknown or not val.types:
                reasons.update(val.unknown or {f"type of field {name} is unresolved"})
    status = "FAIL" if failures else "UNRESOLVED" if reasons else "PASS"
    finding = Finding(code="shape-agreement", message="every individually analyzed shape agrees with the declared fields and JSON types", location=location)
    return ContractResult(id=contract.id, kind=contract.kind, status=status, findings=failures or ([finding] if status == "PASS" else []), unresolved=sorted(reasons)), engine.edges


def analyze_sources(files: dict[str, str], config: Config | dict[str, Any]) -> AnalysisReport:
    """Analyze source text as data using the same JSON contracts as the browser.

    Errors and limits are reported, rather than importing applications or silently
    weakening a contract. Paths are project-relative and source hashes canonical.
    """
    from .models import validate_bundle, validate_config
    inspected: dict[str, str] = {}
    configuration = None
    try:
        configuration = validate_config(config)
        bundle = validate_bundle({"version": 1, "files": files, "config": configuration.model_dump(mode="json")})
        inspected = {path: source for path, source in bundle.files.items() if selected(path, configuration)}
        if not inspected:
            raise SourceError("selected source scope is empty")
        program = Program(inspected, configuration)
    except LimitError as exc:
        return base_report(inspected, configuration or config, status="UNRESOLVED", errors=[Finding(code="analysis-limit", message=str(exc))])
    except (ValidationError, SourceError, TypeError, ValueError, RecursionError) as exc:
        # Validation errors omit raw inputs, which may contain private source.
        message = json.dumps(exc.errors(include_input=False, include_context=False)) if isinstance(exc, ValidationError) else str(exc)
        return base_report(inspected, configuration or config, status="ERROR", errors=[Finding(code="input-error", message=message)])
    results: list[ContractResult] = []
    all_edges: list[GraphEdge] = []
    for contract in configuration.contracts:
        try:
            # Abstract module declarations and registration storage must be fresh
            # for each requirement; one analysis cannot mutate another's inputs.
            program = Program(inspected, configuration)
            if contract.kind == "shape":
                result, edges = shape_result(program, contract)
            else:
                engine = Engine(program, contract)
                value, state = engine.run(contract.entry)
                witness: list[GraphEdge] = []
                findings: list[Finding] = []
                if contract.kind == "connection":
                    proven = contract.target in engine.visited
                    if proven:
                        status = "PASS"
                        witness = engine.witness(contract.entry, contract.target)
                        findings.append(Finding(code="required-connection", message=f"supported structural path reaches {contract.target}", location=witness[-1].location if witness else None))
                    else:
                        status = "UNRESOLVED" if engine.blocked else "FAIL"
                        findings.append(Finding(code="missing-connection", message=f"no supported structural path from {contract.entry} to {contract.target}"))
                else:
                    tokens = {token for token, _ in state.produced if token.startswith(contract.producer + "@")}
                    delivered = {token for token, field in state.delivered if contract.field is None or field == contract.field}
                    paths = engine.path_states or [state]
                    complete_paths = []
                    for path in paths:
                        produced_on_path = {token for token, _ in path.produced if token.startswith(contract.producer + "@")}
                        delivered_on_path = {token for token, field in path.delivered if contract.field is None or field == contract.field}
                        complete = bool(produced_on_path) and produced_on_path <= delivered_on_path
                        complete_paths.append(complete)
                        if not complete and not path.uncertain:
                            engine.handoff_bad = True
                    proven = all(complete_paths) and bool(tokens) and tokens <= delivered and not engine.handoff_bad
                    if proven:
                        status = "PASS"
                        destination = contract.consumer or contract.entry
                        witness = engine.witness(contract.entry, contract.producer) + [edge for edge in engine.edges if edge.kind == "handoff"]
                        findings.append(Finding(code="required-handoff", message=f"identified producer result reaches {destination}"))
                    else:
                        status = "FAIL" if engine.handoff_bad else "UNRESOLVED" if engine.blocked else "FAIL"
                        message = "producer result is not delivered on every analyzed branch" if engine.handoff_bad else "identified producer result does not reach the declared destination"
                        findings.append(Finding(code="missing-handoff", message=message))
                # Unrelated uncertainty is retained as scope information without
                # invalidating a proven supported structural path.
                result = ContractResult(id=contract.id, kind=contract.kind, status=status, findings=findings, witness=witness, unresolved=sorted(engine.reasons))
                edges = engine.edges
            results.append(result)
            all_edges.extend(edge for edge in edges if edge not in all_edges)
        except LimitError as exc:
            results.append(ContractResult(id=contract.id, kind=contract.kind, status="UNRESOLVED", unresolved=[str(exc)]))
        except (SourceError, RecursionError) as exc:
            results.append(ContractResult(id=contract.id, kind=contract.kind, status="ERROR", findings=[Finding(code="selected-input-error", message=str(exc))]))
    return base_report(inspected, configuration, status=status_of([result.status for result in results]), contracts=results,
                       edges=sorted(all_edges, key=lambda e: (e.source, e.location.path, e.location.line, e.location.column, e.target, e.kind)),
                       assumptions=["Source-only structural and dependency evidence; application code is never imported or executed.",
                                    "Connection means a supported potential path, not all-branch invocation or successful execution.",
                                    "Handoff follows identified producer values on analyzed paths; unknown dispatch remains unresolved.",
                                    "Prefect scheduling and future delivery are modeled structurally, without observing execution or map cardinality.",
                                    "Typer registration follows application ownership; selected runtime command and callback options are not observed.",
                                    "Pydantic declarations describe field shape; successful runtime validation is not established."])


def analyze_project(root: Path | str, config: Config | dict[str, Any]) -> AnalysisReport:
    """Read selected project files; fail on unreadable inputs or escaping links."""
    from .models import validate_config
    files: dict[str, str] = {}
    try:
        configuration = validate_config(config)
        base = Path(root).resolve(strict=True)
        paths = set()
        for source_root in configuration.source_roots:
            folder = base / source_root
            if not folder.is_dir():
                raise SourceError(f"selected source root {source_root} is not a directory")
            # os.walk does not follow symlink directories or enter generated
            # environments; explicit selected artifacts are handled below.
            import os
            for directory, dirs, names in os.walk(folder, followlinks=False):
                dirs[:] = sorted(name for name in dirs if name not in (".git", ".venv", "node_modules", "__pycache__", "dist", "build", "public") and not (Path(directory) / name).is_symlink())
                for name in sorted(names):
                    path = Path(directory) / name
                    relative = path.relative_to(base).as_posix()
                    if name.endswith(".py") and selected(relative, configuration):
                        paths.add(relative)
        for contract in configuration.contracts:
            if contract.kind == "shape" and contract.mode == "artifact":
                paths.add(contract.source)
        for relative in sorted(paths):
            path = base / relative
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts or not path.resolve(strict=True).is_relative_to(base):
                raise SourceError(f"selected input {relative} escapes project root")
            files[relative] = path.read_text(encoding="utf-8")
        return analyze_sources(files, configuration)
    except (OSError, UnicodeError, SourceError, ValidationError, ValueError) as exc:
        message = json.dumps(exc.errors(include_input=False, include_context=False)) if isinstance(exc, ValidationError) else str(exc)
        return base_report(files, config, status="ERROR", errors=[Finding(code="read-error", message=message)])
