"""Deterministic source census and explicit accounting, never application execution."""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import (Config, Contract, ContractResult, CoverageContract, CoverageInventory,
                     CoverageSurface, Finding, GraphEdge, SourceLocation, SurfaceKind)

if TYPE_CHECKING:
    from .analyzer import Program


@dataclass(frozen=True)
class Application:
    symbol: str


class CensusError(ValueError):
    pass


class CensusLimitError(CensusError):
    pass


def in_roots(path: str, roots: list[str]) -> bool:
    return any(root == "." or path.startswith(root + "/") for root in roots)


def coverage_roots(config: Config) -> list[str]:
    return sorted({root for contract in config.contracts if contract.kind == "coverage" for root in contract.roots})


class Census(ast.NodeVisitor):
    def __init__(self, program: Program, module: str, steps: list[int]):
        self.program = program
        self.module = module
        self.path = program.paths[module]
        self.scopes: list[tuple[str, str, dict[str, str | Application | None]]] = [("", "module", {})]
        self.surfaces: dict[str, CoverageSurface] = {}
        self.steps = steps
        self.assigned_applications: set[int] = set()

    def visit(self, node: ast.AST) -> None:
        self.steps[0] += 1
        if self.steps[0] > self.program.config.max_steps:
            raise CensusLimitError(f"coverage analysis step limit {self.program.config.max_steps} exceeded")
        super().visit(node)

    def symbol(self, name: str) -> str:
        return self.module + ":" + self.scopes[-1][0] + name

    def resolve(self, node: ast.AST) -> str | Application | None:
        if isinstance(node, ast.Name):
            for _, _, bindings in reversed(self.scopes):
                if node.id in bindings:
                    return bindings[node.id]
            return self.program.resolve_text(self.module, node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            return base + "." + node.attr if isinstance(base, str) else None
        return None

    def add(self, identity: str, kind: SurfaceKind, symbol: str, node: ast.AST, reasons: list[str] | None = None) -> None:
        if identity in self.surfaces:
            raise CensusError(f"duplicate coverage declaration identity {identity}")
        self.surfaces[identity] = CoverageSurface(id=identity, kind=kind, symbol=symbol,
            location=SourceLocation(path=self.path, line=node.lineno, column=node.col_offset), unresolved=reasons or [])

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.scopes[-1][2][alias.asname or alias.name.split(".")[0]] = "ext:" + (alias.name if alias.asname else alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = self.program.imported(self.module, node)
        for alias in node.names:
            if alias.name == "*":
                if module in ("prefect", "typer"):
                    symbol = self.symbol("framework-import@" + str(node.lineno))
                    self.add(symbol, "registration", symbol, node, ["wildcard framework imports obscure registration bindings"])
                continue
            target = module + ":" + alias.name if module in self.program.modules else "ext:" + module + "." + alias.name
            self.scopes[-1][2][alias.asname or alias.name] = target

    def registration(self, symbol: str, node: ast.AST) -> None:
        target = node.func if isinstance(node, ast.Call) else node
        resolved = self.resolve(target)
        if resolved in ("ext:prefect.flow", "ext:prefect.task"):
            self.add(symbol + "@" + resolved.removeprefix("ext:"), "registration", symbol, node)
        elif isinstance(target, ast.Attribute) and target.attr in ("command", "callback"):
            owner = self.resolve(target.value)
            reasons = [] if isinstance(owner, Application) else ["Typer registration ownership is unresolved"]
            self.add(symbol + "@typer." + target.attr, "registration", symbol, node, reasons)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        symbol = self.symbol(node.name)
        self.add(symbol, "method" if self.scopes[-1][1] == "class" else "function", symbol, node)
        for decorator in node.decorator_list:
            self.registration(symbol, decorator)
            self.visit(decorator)
        for default in node.args.defaults + [default for default in node.args.kw_defaults if default is not None]:
            self.visit(default)
        self.scopes[-1][2][node.name] = symbol
        parameters = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        bindings = {argument.arg: None for argument in parameters}
        indexed = self.program.functions.get(symbol)
        if indexed is not None:
            bindings.update({name: None for name in indexed.locals})
        if node.args.vararg:
            bindings[node.args.vararg.arg] = None
        if node.args.kwarg:
            bindings[node.args.kwarg.arg] = None
        self.scopes.append((symbol.partition(":")[2] + ".<locals>.", "function", bindings))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = self.symbol(node.name)
        existing = self.program.classes.get(symbol)
        bases = [self.resolve(base) for base in node.bases]
        model = bool(existing and existing.model) or any(base == "ext:pydantic.BaseModel" or isinstance(base, str) and base in self.surfaces and self.surfaces[base].kind == "model" for base in bases)
        self.add(symbol, "model" if model else "class", symbol, node)
        for decorator in node.decorator_list:
            self.registration(symbol, decorator)
            self.visit(decorator)
        self.scopes[-1][2][node.name] = symbol
        self.scopes.append((symbol.partition(":")[2] + ".", "class", {}))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def exports(self, node: ast.AST, value: ast.AST | None) -> None:
        symbol = self.module + ":__all__"
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)) and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts):
            for item in value.elts:
                self.add(symbol + "[" + json.dumps(item.value, ensure_ascii=False) + "]", "export", symbol, item)
        else:
            self.add(symbol, "export", symbol, node, ["public export members are not a literal string set"])

    def assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        reference = self.resolve(node.value) if node.value is not None else None
        if isinstance(node.value, ast.Call) and self.resolve(node.value.func) == "ext:typer.Typer":
            target = targets[0]
            symbol = self.symbol(target.id) if isinstance(target, ast.Name) else self.symbol("application@" + str(node.lineno) + ":" + str(node.col_offset))
            reasons = [] if isinstance(target, ast.Name) else ["application assignment ownership is unresolved"]
            self.add(symbol + "@typer.application", "registration", symbol, node, reasons)
            reference = Application(symbol)
            self.assigned_applications.add(id(node.value))
        elif isinstance(node.value, ast.Call) and self.resolve(node.value.func) == "ext:pydantic.create_model":
            target = targets[0]
            symbol = self.symbol(target.id) if isinstance(target, ast.Name) else self.symbol("model@" + str(node.lineno) + ":" + str(node.col_offset))
            reasons = [] if isinstance(target, ast.Name) else ["dynamic model assignment ownership is unresolved"]
            self.add(symbol, "model", symbol, node, reasons)
            reference = symbol
        for target in targets:
            if isinstance(target, ast.Name):
                if target.id == "__all__" and self.scopes[-1][1] == "module":
                    self.exports(node, node.value)
                self.scopes[-1][2][target.id] = reference
        if node.value is not None:
            self.visit(node.value)

    visit_Assign = assignment
    visit_AnnAssign = assignment

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name) and node.target.id == "__all__" and self.scopes[-1][1] == "module":
            self.add(self.module + ":__all__@mutation:" + str(node.lineno), "export", self.module + ":__all__", node, ["mutated public export membership is unresolved"])
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.resolve(node.func) == "ext:typer.Typer" and id(node) not in self.assigned_applications:
            symbol = self.symbol("application@" + str(node.lineno) + ":" + str(node.col_offset))
            self.add(symbol + "@typer.application", "registration", symbol, node, ["application construction has no statically declared owner"])
        if isinstance(node.func, ast.Attribute) and node.func.attr == "add_typer":
            parent = self.resolve(node.func.value)
            child = self.resolve(node.args[0]) if node.args else next((self.resolve(keyword.value) for keyword in node.keywords if keyword.arg == "typer_instance"), None)
            owner = parent.symbol if isinstance(parent, Application) else self.symbol("attachment@" + str(node.lineno) + ":" + str(node.col_offset))
            target = child.symbol if isinstance(child, Application) else "<unresolved>"
            reasons = [] if isinstance(parent, Application) and isinstance(child, Application) else ["Typer attachment ownership is unresolved"]
            self.add(owner + "@typer.add_typer[" + json.dumps(target, ensure_ascii=False) + "]", "registration", owner, node, reasons)
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "__all__" and self.scopes[-1][1] == "module":
            self.add(self.module + ":__all__@mutation:" + str(node.lineno), "export", self.module + ":__all__", node, ["mutated public export membership is unresolved"])
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        original = self.scopes[-1][2].copy()
        results = []
        for branch in (node.body, node.orelse):
            self.scopes[-1][2].clear()
            self.scopes[-1][2].update(original)
            for statement in branch:
                self.visit(statement)
            results.append(self.scopes[-1][2].copy())
        self.scopes[-1][2].clear()
        for name in results[0].keys() | results[1].keys():
            first = results[0].get(name)
            self.scopes[-1][2][name] = first if first == results[1].get(name) else None


def applicable(surface: CoverageSurface, contract: Contract) -> bool:
    if surface.kind == "registration" and ("@typer.application" in surface.id or "@typer.add_typer[" in surface.id):
        return False
    if surface.kind in ("class", "model"):
        return surface.kind == "model" and contract.kind == "shape" and contract.mode == "model" and contract.source == surface.symbol
    if surface.kind == "export":
        if contract.kind != "shape" or contract.mode != "exports" or contract.source != surface.symbol:
            return False
        if surface.id.startswith(surface.symbol + "["):
            return json.loads(surface.id[len(surface.symbol) + 1:-1]) in contract.fields
        return True
    if contract.kind == "connection":
        return surface.symbol in (contract.entry, contract.target)
    if contract.kind == "handoff":
        return surface.symbol in (contract.entry, contract.producer, contract.consumer)
    return contract.kind == "shape" and contract.mode == "mapping" and contract.source == surface.symbol


def check_coverage(program: Program, contract: CoverageContract) -> tuple[ContractResult, list[GraphEdge]]:
    files = sorted(path for path in program.files if path.endswith(".py") and in_roots(path, contract.roots))
    for root in contract.roots:
        if not any(in_roots(path, [root]) for path in files):
            raise CensusError(f"coverage root {root} has no inspected Python sources")
    surfaces = {}
    steps = [0]
    for module, tree in sorted(program.trees.items()):
        if program.paths[module] not in files:
            continue
        census = Census(program, module, steps)
        census.visit(tree)
        for identity, surface in census.surfaces.items():
            if identity in surfaces:
                raise CensusError(f"duplicate coverage declaration identity {identity}")
            surfaces[identity] = surface
    ledger = {entry.surface: entry for entry in contract.ledger}
    stale = sorted(set(ledger) - set(surfaces))
    findings = [Finding(code="stale-coverage", message=f"ledger surface {identity} no longer exists") for identity in stale]
    unresolved = []
    contracts = {item.id: item for item in program.config.contracts}
    for identity, surface in sorted(surfaces.items()):
        entry = ledger.get(identity)
        unresolved.extend(f"{identity}: {reason}" for reason in surface.unresolved)
        if entry is None:
            if not surface.unresolved:
                findings.append(Finding(code="unclassified-surface", message=f"{surface.kind} {identity} has no contract association or internal reason", location=surface.location))
        elif entry.kind == "internal":
            surface.classification = "internal"
            surface.reason = entry.reason
        else:
            for identifier in entry.contracts:
                if not applicable(surface, contracts[identifier]):
                    raise CensusError(f"contract {identifier} does not explicitly name applicable surface {identity}")
            surface.classification = "contract"
            surface.contracts = entry.contracts
    inventory = CoverageInventory(roots=contract.roots, inspected_files=files, surfaces=[surfaces[key] for key in sorted(surfaces)], stale=stale)
    status = "FAIL" if findings else "UNRESOLVED" if unresolved else "PASS"
    if not findings:
        findings = [Finding(code="coverage-accounting", message=f"{len(surfaces)} discovered surfaces accounted for; classification does not prove runtime behavior")]
    return ContractResult(id=contract.id, kind="coverage", status=status, findings=findings, unresolved=sorted(set(unresolved)), coverage=inventory), []
