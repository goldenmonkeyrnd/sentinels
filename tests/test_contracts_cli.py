from contextlib import redirect_stdout
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from concurrent.futures import Future

from pydantic import ValidationError

from sentinels import __version__
from sentinels.cli import main
from sentinels.models import AnalysisReport, Bundle, Config, validate_config


def config_data():
    return {"version": 1, "contracts": [{"kind": "connection", "id": "validation", "entry": "app:main", "target": "app:validate"}]}


class ContractValidationTests(unittest.TestCase):
    def test_strict_boundary_rejects_coercion_and_unknown_fields(self):
        for mutate in (
            lambda data: data.update(version=True),
            lambda data: data.update(max_nodes="100"),
            lambda data: data.update(unknown=True),
            lambda data: data["contracts"][0].update(typo="target"),
        ):
            data = config_data()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValidationError):
                validate_config(data)

    def test_empty_and_duplicate_contracts_are_invalid(self):
        for contracts in ([], config_data()["contracts"] * 2):
            with self.subTest(contracts=contracts), self.assertRaises(ValidationError):
                validate_config({"contracts": contracts})

    def test_handoff_requires_one_complete_destination(self):
        base = {"kind": "handoff", "id": "result", "entry": "app:main", "producer": "app:calculate"}
        for destination in ({}, {"consumer": "app:publish"}, {"argument": "result"}, {"consumer": "app:publish", "argument": "result", "returned_field": "result"}):
            with self.subTest(destination=destination), self.assertRaises(ValidationError):
                validate_config({"contracts": [base | destination]})
        validate_config({"contracts": [base | {"consumer": "app:publish", "argument": 0}]})
        validate_config({"contracts": [base | {"returned_field": "result"}]})

    def test_nullable_and_optional_presence_remain_distinct(self):
        config = validate_config({"contracts": [{"kind": "shape", "id": "shape", "source": "app:build", "fields": {"value": {"type": "integer", "required": False, "nullable": True}}}]})
        field = config.contracts[0].fields["value"]
        self.assertFalse(field.required)
        self.assertTrue(field.nullable)

    def test_bundle_rejects_escaping_and_duplicate_normalized_paths(self):
        for files in ({"../app.py": ""}, {"/app.py": ""}, {"app.py": "", "./app.py": ""}, {"dir\\app.py": ""}):
            with self.subTest(files=files), self.assertRaises(ValidationError):
                Bundle.model_validate_json(json.dumps({"files": files, "config": config_data()}))

    def test_schema_contracts_are_discriminated_and_closed(self):
        schema = Config.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["contracts"]["items"]["discriminator"]["propertyName"], "kind")

    def test_json_duplicate_file_identity_is_rejected_before_parsing(self):
        data = '{"files":{"app.py":"first","app.py":"second"},"config":' + json.dumps(config_data()) + "}"
        with self.assertRaisesRegex(ValueError, "duplicate JSON key: app.py"):
            Bundle.model_validate_json(data)


class CLITests(unittest.TestCase):
    def run_cli(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, output.getvalue()

    def test_invalid_config_is_error_with_machine_readable_report(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sentinels.toml").write_text("contracts = []\n", encoding="utf-8")
            code, output = self.run_cli(["check", "--root", directory, "--json"])
        self.assertEqual(code, 3)
        report = AnalysisReport.model_validate_json(output)
        self.assertEqual(report.status, "ERROR")
        self.assertEqual(report.errors[0].code, "INPUT_ERROR")

    def test_missing_bundle_and_conflicting_options_are_error(self):
        for arguments in (["check", "--bundle", "/missing/sentinels.json", "--json"], ["check", "--bundle", "bundle.json", "--root", ".", "--json"], ["check", "--unknown", "--json"]):
            with self.subTest(arguments=arguments):
                code, output = self.run_cli(arguments)
                self.assertEqual(code, 3)
                self.assertEqual(json.loads(output)["status"], "ERROR")

    def test_bundle_passes_only_data_to_analyzer_and_preserves_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "project.json")
            files = {"app.py": "raise RuntimeError('must not execute')\n"}
            path.write_text(json.dumps({"files": files, "config": config_data()}), encoding="utf-8")
            report = AnalysisReport(checker_version=__version__, parser_version="3.13", source_hash="source", config_hash="config", status="UNRESOLVED")
            with patch("sentinels.cli.analyze_sources", return_value=report) as analyze:
                code, output = self.run_cli(["check", "--bundle", str(path), "--json"])
                analyze.assert_called_once()
                self.assertEqual(analyze.call_args.args[0], files)
                self.assertIsInstance(analyze.call_args.args[1], Config)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["status"], "UNRESOLVED")

    def test_real_project_fault_and_repair_reach_the_analyzer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sentinels.toml").write_text(
                '[[contracts]]\nkind="connection"\nid="validation"\nentry="app:main"\ntarget="app:validate"\n', encoding="utf-8"
            )
            source = root / "app.py"
            source.write_text("def validate():\n    return True\ndef main():\n    return {}\n", encoding="utf-8")
            code, output = self.run_cli(["check", "--root", directory, "--json"])
            self.assertEqual(code, 1, output)
            self.assertEqual(json.loads(output)["contracts"][0]["status"], "FAIL")
            source.write_text("def validate():\n    return True\ndef main():\n    return validate()\n", encoding="utf-8")
            code, output = self.run_cli(["check", "--root", directory, "--json"])
            self.assertEqual(code, 0, output)
            self.assertEqual(json.loads(output)["contracts"][0]["witness"][0]["target"], "app:validate")


@unittest.skipUnless(importlib.util.find_spec("prefect") and importlib.util.find_spec("typer"), "install the pinned adapters extra")
class NativeAdapterTests(unittest.TestCase):
    """Exercise upstream API assumptions without starting orchestration services."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"PREFECT_HOME": self.directory.name, "PREFECT_SERVER_ANALYTICS_ENABLED": "false"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_prefect_options_preserve_function_and_fn_bypasses_engine(self):
        import prefect
        from prefect import flow, task

        self.assertEqual(prefect.__version__, "3.6.11")
        calls = []

        @task
        def calculate(value: int) -> int:
            calls.append(value)
            return value * 2

        configured = calculate.with_options(name="configured-calculation")

        @flow
        def publication(value: int) -> int:
            return configured.fn(value)

        self.assertIs(configured.fn, calculate.fn)
        self.assertEqual(configured.name, "configured-calculation")
        self.assertEqual(publication.fn(21), 42)
        self.assertEqual(calls, [21])
        self.assertTrue(callable(calculate.submit))
        self.assertTrue(callable(calculate.map))

    def test_prefect_wait_supplies_no_payload_but_result_does(self):
        from prefect.futures import PrefectConcurrentFuture

        wrapped = Future()
        payload = {"population": 42}
        wrapped.set_result(payload)
        future = PrefectConcurrentFuture(task_run_id=uuid4(), wrapped_future=wrapped)
        self.assertIsNone(future.wait())
        self.assertIs(future.result(), payload)

    def test_typer_callback_replacement_and_nested_ownership(self):
        import typer
        from typer.testing import CliRunner

        self.assertEqual(typer.__version__, "0.20.0")
        calls = []

        def old_callback():
            calls.append("old")

        app = typer.Typer(callback=old_callback)
        child = typer.Typer()
        unattached = typer.Typer()

        @app.callback()
        def replacement():
            calls.append("replacement")

        @child.command()
        def publish():
            calls.append("publish")

        @unattached.command()
        def hidden():
            calls.append("hidden")

        app.add_typer(child, name="county")
        result = CliRunner().invoke(app, ["county", "publish"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls, ["replacement", "publish"])
        self.assertIs(app.registered_callback.callback, replacement)
        self.assertIs(app.registered_groups[0].typer_instance, child)
        result = CliRunner().invoke(app, ["hidden"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn("hidden", calls)


if __name__ == "__main__":
    unittest.main()
