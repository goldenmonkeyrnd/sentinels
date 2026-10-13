from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinels import analyze_project, analyze_sources
from sentinels.analyzer import RULE_HANDLERS
from sentinels.cli import main
from sentinels.models import AnalysisReport, Config, ContractResult


SOURCE = 'def validate(): pass\ndef main(): validate()\n'
CONNECTION = {'kind': 'connection', 'id': 'validation', 'entry': 'app:main', 'target': 'app:validate'}


def internal(surface, reason='implementation helper; not a declared production seam'):
    return {'kind': 'internal', 'surface': surface, 'reason': reason}


def association(surface, *contracts):
    return {'kind': 'contract', 'surface': surface, 'contracts': list(contracts)}


def configured(ledger=None, rules=None, **coverage):
    rule = {'kind': 'coverage', 'id': 'production', 'roots': ['.'], 'ledger': ledger if ledger is not None else [association('app:main', 'validation'), association('app:validate', 'validation')], **coverage}
    return {'version': 2, 'contracts': [*(rules if rules is not None else [CONNECTION]), rule]}


def check(source=SOURCE, config=None, files=None):
    return analyze_sources(files or {'app.py': source}, config or configured())


class CoverageTests(unittest.TestCase):
    def test_accounting_does_not_claim_behavior(self):
        report = check()
        result = report.contracts[-1]
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(result.coverage.inspected_files, ['app.py'])
        self.assertEqual([(item.id, item.classification) for item in result.coverage.surfaces], [('app:main', 'contract'), ('app:validate', 'contract')])
        disconnected = check(SOURCE.replace('def main(): validate()', 'def main(): pass'))
        self.assertEqual(disconnected.contracts[-1].status, 'PASS')
        self.assertEqual(disconnected.status, 'FAIL')

    def test_new_private_function_fault_and_explicit_repair(self):
        source = SOURCE + 'def _county_label(): return "County"\n'
        report = check(source)
        self.assertEqual(report.status, 'FAIL')
        self.assertEqual(report.contracts[-1].findings[0].code, 'unclassified-surface')
        ledger = configured()['contracts'][-1]['ledger'] + [internal('app:_county_label')]
        repaired = check(source, configured(ledger))
        self.assertEqual(repaired.status, 'PASS')
        self.assertEqual(check(source, configured(ledger)).model_dump(), repaired.model_dump())

    def test_every_nested_conditional_class_and_method_is_censused(self):
        source = SOURCE + 'if condition:\n    def _conditional(): pass\nclass Worker:\n    def _work(self):\n        def nested(): pass\n        return nested()\n'
        missing = check(source)
        items = {item.id: item.kind for item in missing.contracts[-1].coverage.surfaces}
        self.assertEqual(items, {'app:main': 'function', 'app:validate': 'function', 'app:_conditional': 'function', 'app:Worker': 'class', 'app:Worker._work': 'method', 'app:Worker._work.<locals>.nested': 'function'})
        ledger = configured()['contracts'][-1]['ledger'] + [internal(identity) for identity in items if identity not in ('app:main', 'app:validate')]
        self.assertEqual(check(source, configured(ledger)).status, 'PASS')

    def test_conditional_declarations_are_not_unconditional_call_proofs(self):
        source = 'if condition:\n    def validate(): pass\ndef main(): validate()\n'
        report = check(source)
        self.assertEqual(report.contracts[0].status, 'UNRESOLVED')
        self.assertEqual(report.contracts[-1].status, 'PASS')

    def test_stale_and_renamed_surface_fail_with_typed_inventory(self):
        ledger = configured()['contracts'][-1]['ledger'] + [internal('app:removed')]
        report = check(config=configured(ledger))
        self.assertEqual(report.status, 'FAIL')
        self.assertEqual(report.contracts[-1].coverage.stale, ['app:removed'])
        self.assertEqual(AnalysisReport.model_validate_json(report.model_dump_json()), report)

    def test_unrelated_association_and_ordinary_class_cannot_claim_contract(self):
        source = SOURCE + 'def helper(): pass\nclass Worker: pass\n'
        for candidate in ('app:helper', 'app:Worker'):
            ledger = configured()['contracts'][-1]['ledger'] + [association(candidate, 'validation')]
            with self.subTest(candidate=candidate):
                self.assertEqual(check(source, configured(ledger)).status, 'ERROR')

    def test_models_and_literal_exports_need_applicable_shape_contracts(self):
        source = SOURCE + 'from pydantic import BaseModel as Model\nclass Publication(Model):\n    count: int\n__all__ = ["main", "Publication"]\n'
        rules = [CONNECTION,
                 {'kind': 'shape', 'id': 'publication-model', 'mode': 'model', 'source': 'app:Publication', 'fields': {'count': {'type': 'integer'}}},
                 {'kind': 'shape', 'id': 'exports', 'mode': 'exports', 'source': 'app:__all__', 'fields': {'main': {'type': 'string'}, 'Publication': {'type': 'string'}}}]
        ledger = configured()['contracts'][-1]['ledger'] + [association('app:Publication', 'publication-model'), association('app:__all__["main"]', 'exports'), association('app:__all__["Publication"]', 'exports')]
        report = check(source, configured(ledger, rules))
        self.assertEqual(report.status, 'PASS')
        self.assertEqual({item.id: item.kind for item in report.contracts[-1].coverage.surfaces}['app:Publication'], 'model')
        renamed = check(source.replace('"Publication"]', '"Renamed"]'), configured(ledger, rules))
        self.assertEqual(renamed.status, 'FAIL')
        self.assertIn('app:__all__["Publication"]', renamed.contracts[-1].coverage.stale)

    def test_dynamic_exports_and_ownership_do_not_disappear(self):
        source = SOURCE + '__all__ = exported_names()\n@unknown.command()\ndef extra(): pass\n'
        ledger = configured()['contracts'][-1]['ledger'] + [internal('app:__all__'), internal('app:extra'), internal('app:extra@typer.command')]
        report = check(source, configured(ledger))
        self.assertEqual(report.status, 'UNRESOLVED')
        self.assertEqual(len(report.contracts[-1].unresolved), 2)

    def test_dynamic_model_declaration_is_censused_without_claiming_its_schema(self):
        source = SOURCE + 'from pydantic import create_model\nGenerated = create_model("Generated", count=(int, ...))\n'
        self.assertEqual(check(source).status, 'FAIL')
        ledger = configured()['contracts'][-1]['ledger'] + [internal('app:Generated')]
        report = check(source, configured(ledger))
        self.assertEqual(report.status, 'PASS')
        surface = next(item for item in report.contracts[-1].coverage.surfaces if item.id == 'app:Generated')
        self.assertEqual(surface.kind, 'model')

    def test_framework_aliases_applications_attachments_and_replaced_callbacks(self):
        source = SOURCE + 'from prefect import task as job\nfrom typer import Typer as Application\nroot = Application()\nchild = Application()\n@job\ndef calculate(): return 42\n@root.callback()\ndef old(): pass\n@root.callback()\ndef replacement(): pass\n@child.command()\ndef publish(): pass\nroot.add_typer(child)\n'
        missing = check(source)
        ids = {item.id for item in missing.contracts[-1].coverage.surfaces}
        self.assertIn('app:calculate@prefect.task', ids)
        self.assertIn('app:old@typer.callback', ids)
        self.assertIn('app:replacement@typer.callback', ids)
        self.assertIn('app:root@typer.add_typer["app:child"]', ids)
        ledger = [internal(identity) for identity in sorted(ids)]
        self.assertEqual(check(source, configured(ledger)).status, 'PASS')
        ledger = [association(identity, 'validation') if identity == 'app:root@typer.application' else internal(identity) for identity in sorted(ids)]
        self.assertEqual(check(source, configured(ledger)).status, 'ERROR')

    def test_selected_production_roots_include_excluded_private_sources(self):
        files = {'pipeline/build.py': SOURCE, 'pipeline/test_internal.py': 'def _new(): pass\n', 'tools/dev.py': 'def developer(): pass\n'}
        config = configured([], rules=[], roots=['pipeline'])
        config['excludes'] = ['pipeline/test_*.py']
        report = check(config=config, files=files)
        self.assertEqual(report.status, 'FAIL')
        inventory = report.contracts[0].coverage
        self.assertEqual(inventory.inspected_files, ['pipeline/build.py', 'pipeline/test_internal.py'])
        self.assertIn('pipeline.test_internal:_new', [item.id for item in inventory.surfaces])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, contents in files.items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(contents)
            native = analyze_project(root, config)
        self.assertEqual(native.model_dump(), report.model_dump())

    def test_selected_root_symlink_directory_cannot_silently_hide_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pipeline').mkdir()
            (root / 'pipeline/build.py').write_text(SOURCE)
            (root / 'shared').mkdir()
            (root / 'shared/hidden.py').write_text('def hidden(): pass')
            (root / 'pipeline/link').symlink_to(root / 'shared', target_is_directory=True)
            report = analyze_project(root, configured([], rules=[], roots=['pipeline']))
        self.assertEqual(report.status, 'ERROR')
        self.assertIn('symbolic link', report.errors[0].message)

    def test_production_inventory_includes_technical_directory_names(self):
        names = ['build', 'public', 'dist', 'node_modules', '__pycache__', '.venv', '.git']
        config = configured([], rules=[], roots=['pipeline'])
        config['excludes'] = ['pipeline/**']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {f'pipeline/{name}/private.py' for name in names}
            for relative in expected | {'build/unselected.py'}:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('def _hidden(): pass\n')
            report = analyze_project(root, config)
            inventory = report.contracts[0].coverage
            self.assertEqual(report.status, 'FAIL')
            self.assertEqual(set(inventory.inspected_files), expected)
            self.assertEqual({surface.location.path for surface in inventory.surfaces}, expected)
            self.assertTrue(all(surface.classification == 'unclassified' for surface in inventory.surfaces))
            config['contracts'][0]['ledger'] = [internal(surface.id) for surface in inventory.surfaces]
            repaired = analyze_project(root, config)
            self.assertEqual(repaired.status, 'PASS')
            self.assertNotIn('build/unselected.py', repaired.scope)

    def test_empty_roots_duplicate_entries_invalid_associations_and_v1_error(self):
        variants = [configured(roots=[]), configured(roots=['.', '.']), configured([internal('app:main'), internal('app:main')]), configured([internal('app:main', '  ')]), configured([association('app:main', 'absent')]), configured([association('app:main', 'production')]), configured(roots=['../escape']), configured(roots=['./'])]
        for config in variants:
            with self.subTest(config=config):
                self.assertEqual(check(config=config).status, 'ERROR')
        config = configured(); config['version'] = 1
        self.assertEqual(check(config=config).status, 'ERROR')
        self.assertEqual(check(config=configured(roots=['missing'])).status, 'ERROR')

    def test_duplicate_declared_or_export_identity_errors(self):
        for source in (SOURCE + 'if flag:\n    def new(): pass\nelse:\n    def new(): pass\n', SOURCE + '__all__ = ["main", "main"]\n'):
            self.assertEqual(check(source).status, 'ERROR')

    def test_census_keeps_limits_and_never_executes_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            source = f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n' + SOURCE
            self.assertEqual(check(source).status, 'PASS')
            self.assertFalse(marker.exists())
        config = configured(); config['max_nodes'] = 1
        self.assertEqual(check(config=config).status, 'UNRESOLVED')
        config = configured([], rules=[]); config['max_steps'] = 1
        self.assertEqual(check(config=config).status, 'UNRESOLVED')

    def test_dispatcher_registry_and_actual_coverage_fault_repair(self):
        self.assertEqual(set(RULE_HANDLERS), {'connection', 'handoff', 'shape', 'coverage'})
        self.assertEqual(check().status, 'PASS')
        with patch.dict(RULE_HANDLERS):
            del RULE_HANDLERS['coverage']
            self.assertEqual(check().status, 'ERROR')
        source = SOURCE + 'def _new(): pass\n'
        self.assertEqual(check(source).status, 'FAIL')
        def disconnected(program, contract):
            return ContractResult(id=contract.id, kind='coverage', status='PASS'), []
        with patch.dict(RULE_HANDLERS, {'coverage': disconnected}):
            # Demonstrates why registry membership alone cannot qualify wiring.
            self.assertNotEqual(check(source).status, 'FAIL')
        self.assertEqual(check(source).status, 'FAIL')
        ledger = configured()['contracts'][-1]['ledger'] + [internal('app:_new')]
        self.assertEqual(check(source, configured(ledger)).status, 'PASS')

    def test_real_cli_registry_wiring_and_surface_fault_repair(self):
        def invoke(path):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['check', '--bundle', str(path), '--json'])
            return code, AnalysisReport.model_validate_json(output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'project.json'
            source = SOURCE + 'def _new(): pass\n'
            bundle = {'version': 2, 'files': {'app.py': source}, 'config': configured()}
            path.write_text(json.dumps(bundle))
            code, report = invoke(path)
            self.assertEqual((code, report.status), (1, 'FAIL'))
            self.assertEqual(report.contracts[-1].findings[0].code, 'unclassified-surface')
            with patch.dict(RULE_HANDLERS):
                del RULE_HANDLERS['coverage']
                code, report = invoke(path)
                self.assertEqual((code, report.status), (3, 'ERROR'))
            ledger = configured()['contracts'][-1]['ledger'] + [internal('app:_new')]
            bundle['config'] = configured(ledger)
            path.write_text(json.dumps(bundle))
            code, report = invoke(path)
            self.assertEqual((code, report.status), (0, 'PASS'))


if __name__ == '__main__':
    unittest.main()
