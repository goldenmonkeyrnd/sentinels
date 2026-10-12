"""Adversarial inputs that must not acquire a false structural proof."""

import unittest

from sentinels import analyze_sources


class AdversarialBindingTests(unittest.TestCase):
    def connection(self, source, target="app:validate"):
        return analyze_sources({"app.py": source}, {"contracts": [{"kind": "connection", "id": "proof", "entry": "app:main", "target": target}]}).contracts[0]

    def test_constant_short_circuit_never_calls_target(self):
        for expression in ("False and validate()", "True or validate()", "validate() if False else None", "None if True else validate()"):
            with self.subTest(expression=expression):
                result = self.connection(f"def validate(): pass\ndef main():\n    return {expression}\n")
                self.assertNotEqual(result.status, "PASS")

    def test_unconsumed_generator_expression_is_not_an_invocation(self):
        result = self.connection("def validate(): pass\ndef main():\n    result = (validate() for _ in [1])\n")
        self.assertNotEqual(result.status, "PASS")

    def test_generator_creation_does_not_execute_generator_body(self):
        result = self.connection("def validate(): pass\ndef generated():\n    yield validate()\ndef main():\n    result = generated()\n")
        self.assertNotEqual(result.status, "PASS")

    def test_shadowed_staticmethod_is_an_opaque_decorator(self):
        result = self.connection("def validate(): pass\ndef staticmethod(fn):\n    return lambda: None\nclass App:\n    @staticmethod\n    def run():\n        validate()\ndef main():\n    App.run()\n")
        self.assertEqual(result.status, "UNRESOLVED")

    def test_parameter_super_is_not_the_builtin(self):
        result = self.connection("class Base:\n    def validate(self): pass\ndef fake():\n    return object()\nclass Child(Base):\n    def run(self, super):\n        super().validate()\ndef main():\n    Child().run(fake)\n", "app:Base.validate")
        self.assertNotEqual(result.status, "PASS")

    def test_model_base_requires_resolved_pydantic_identity(self):
        for source in (
            "from pydantic import BaseModel\nclass BaseModel: pass\nclass Output(BaseModel):\n    total: int\n",
            "class MyBaseModel: pass\nclass Output(MyBaseModel):\n    total: int\n",
        ):
            with self.subTest(source=source):
                report = analyze_sources({"app.py": source}, {"contracts": [{"kind": "shape", "id": "schema", "mode": "model", "source": "app:Output", "fields": {"total": {"type": "integer"}}}]})
                self.assertNotEqual(report.status, "PASS")

    def test_shadowed_builtin_annotation_does_not_prove_wrong_type(self):
        source = "from pydantic import BaseModel\nstr = int\nclass Output(BaseModel):\n    total: str\n"
        report = analyze_sources({"app.py": source}, {"contracts": [{"kind": "shape", "id": "schema", "mode": "model", "source": "app:Output", "fields": {"total": {"type": "string"}}}]})
        self.assertNotEqual(report.status, "PASS")

    def test_module_monkeypatch_does_not_retain_old_method_body(self):
        result = self.connection("def validate(): pass\nclass Service:\n    def run(self):\n        validate()\ndef replacement(self): pass\nService.run = replacement\ndef main():\n    Service().run()\n")
        self.assertNotEqual(result.status, "PASS")

    def test_module_conditional_rebind_does_not_retain_old_callable(self):
        result = self.connection("def validate(): pass\ndef other(): pass\nif True:\n    validate = other\ndef main():\n    validate()\n")
        self.assertNotEqual(result.status, "PASS")

    def test_class_body_replacement_does_not_retain_old_method_body(self):
        result = self.connection("def validate(): pass\ndef replacement(self): pass\nclass Service:\n    def run(self): validate()\n    run = replacement\ndef main(): Service().run()\n")
        self.assertNotEqual(result.status, "PASS")

    def test_inherited_custom_new_does_not_prove_receiver_identity(self):
        result = self.connection("def validate(): pass\nclass Base:\n    def __new__(cls): return object()\nclass Service(Base):\n    def run(self): validate()\ndef main(): Service().run()\n")
        self.assertNotEqual(result.status, "PASS")


class AdversarialHandoffTests(unittest.TestCase):
    def handoff(self, body, field):
        source = 'def calculate():\n    return {"count":42}\ndef publish(value): pass\ndef main():\n    result = calculate()\n    ' + body + "\n"
        return analyze_sources({"app.py": source}, {"contracts": [{"kind": "handoff", "id": "value", "entry": "app:main", "producer": "app:calculate", "consumer": "app:publish", "argument": "value", "field": field}]}).contracts[0]

    def test_replaced_field_does_not_retain_original_value_provenance(self):
        result = self.handoff('result["count"] = 0\n    publish(result["count"])', "count")
        self.assertNotEqual(result.status, "PASS")

    def test_missing_field_get_fallback_is_not_the_producer_field(self):
        result = self.handoff('publish(result.get("missing",0))', "missing")
        self.assertNotEqual(result.status, "PASS")

    def test_missing_field_subscript_is_not_the_producer_field(self):
        result = self.handoff('publish(result["missing"])', "missing")
        self.assertNotEqual(result.status, "PASS")


class AdversarialModelProjectionTests(unittest.TestCase):
    def test_defaults_and_field_projection_do_not_invent_output_types(self):
        for declaration in (
            "None",
            '"wrong"',
            "Field(default=42, exclude=True)",
            'Field(default_factory=lambda: "wrong")',
        ):
            with self.subTest(declaration=declaration):
                source = "from pydantic import BaseModel, Field\nclass Output(BaseModel):\n    count: int = " + declaration + "\ndef build():\n    return Output().model_dump()\n"
                report = analyze_sources({"app.py": source}, {"contracts": [{"kind": "shape", "id": "output", "mode": "mapping", "source": "app:build", "fields": {"count": {"type": "integer"}}}]})
                self.assertNotEqual(report.status, "PASS")


if __name__ == "__main__":
    unittest.main()
