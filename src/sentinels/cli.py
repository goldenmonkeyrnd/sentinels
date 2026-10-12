"""Command-line boundary for reproducible project and browser-bundle checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import tomllib

from pydantic import ValidationError

from . import __version__, analyze_project, analyze_sources
from .models import AnalysisReport, Bundle, Finding, validate_config

EXIT_CODES = {"PASS": 0, "FAIL": 1, "UNRESOLVED": 2, "ERROR": 3}


class CLIUsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIUsageError(message)


def _parser() -> Parser:
    parser = Parser(description="Check declared source integration requirements; never run the application.")
    parser.add_argument("--version", action="version", version=f"sentinels {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    check = commands.add_parser("check", help="inspect a project or an exported browser project bundle")
    check.add_argument("--root", type=Path)
    check.add_argument("--config", type=Path)
    check.add_argument("--bundle", type=Path)
    check.add_argument("--json", action="store_true", help="write a versioned JSON report to stdout")
    return parser


def _error_report(message: str) -> AnalysisReport:
    empty_hash = hashlib.sha256(b"").hexdigest()
    return AnalysisReport(
        checker_version=__version__, parser_version=platform.python_version(),
        source_hash=empty_hash, config_hash=empty_hash, status="ERROR",
        errors=[Finding(code="INPUT_ERROR", message=message)],
    )


def _input_error_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'input'}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
    return str(error)


def _render(report: AnalysisReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
        return
    print(f"{report.status}: {len(report.contracts)} contract(s), {len(report.scope)} inspected file(s)")
    for error in report.errors:
        print(f"  {error.code}: {error.message}")
    for result in report.contracts:
        print(f"  {result.status} {result.id} ({result.kind})")
        for finding in result.findings:
            location = finding.location
            prefix = f"{location.path}:{location.line}:{location.column + 1}: " if location else ""
            print(f"    {prefix}{finding.code}: {finding.message}")
        for edge in result.witness:
            print(f"    {edge.source} -> {edge.target} [{edge.kind}] at {edge.location.path}:{edge.location.line}")
        for reason in result.unresolved:
            print(f"    unresolved: {reason}")
    print("  Evidence is structural; successful runtime execution is not established.")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    as_json = "--json" in arguments
    try:
        options = _parser().parse_args(arguments)
        as_json = options.json
        if options.bundle is not None:
            if options.root is not None or options.config is not None:
                raise CLIUsageError("--bundle cannot be combined with --root or --config")
            bundle = Bundle.model_validate_json(options.bundle.read_text(encoding="utf-8"))
            report = analyze_sources(bundle.files, bundle.config)
        else:
            root = (options.root or Path(".")).resolve()
            config_path = options.config or Path("sentinels.toml")
            if not config_path.is_absolute():
                config_path = root / config_path
            data = config_path.read_text(encoding="utf-8")
            config = validate_config(json.loads(data) if config_path.suffix == ".json" else tomllib.loads(data))
            report = analyze_project(root, config)
    except (OSError, UnicodeError, ValidationError, ValueError, TypeError) as error:
        report = _error_report(_input_error_message(error))
    _render(report, as_json)
    return EXIT_CODES[report.status]
