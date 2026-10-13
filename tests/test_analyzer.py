from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from sentinels import analyze_sources, analyze_project


def check(source, contract=None, extra=None):
    files = {'app.py': source, **(extra or {})}
    config = {'contracts': [contract or {'kind': 'connection', 'id': 'check', 'entry': 'app:main', 'target': 'app:validate'}]}
    return analyze_sources(files, config)


def handoff(**kwargs):
    return {'kind': 'handoff', 'id': 'flow', 'entry': 'app:main', 'producer': 'app:produce', 'consumer': 'app:consume', 'argument': 'payload', **kwargs}


def shape(**kwargs):
    return {'kind': 'shape', 'id': 'shape', 'source': 'app:publish', 'fields': {'name': {'type': 'string'}, 'count': {'type': 'integer'}}, **kwargs}


class BindingTests(unittest.TestCase):
    def test_direct_call_and_source_witness(self):
        report = check('def validate(): pass\ndef main(): validate()')
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.contracts[0].witness[-1].location.line, 2)

    def test_import_alias_and_relative_import(self):
        files = {'pkg/__init__.py': '', 'pkg/validate.py': 'def validator(): pass', 'pkg/main.py': 'from .validate import validator as run\ndef main(): run()'}
        report = analyze_sources(files, {'contracts': [{'kind': 'connection', 'id': 'c', 'entry': 'pkg.main:main', 'target': 'pkg.validate:validator'}]})
        self.assertEqual(report.status, 'PASS')

    def test_module_qualified_import(self):
        report = check('import local as v\ndef main(): v.validate()', extra={'local.py': 'def validate(): pass'})
        self.assertEqual(report.status, 'FAIL')
        report = check('import local as v\ndef main(): v.validate()', {'kind': 'connection', 'id': 'c', 'entry': 'app:main', 'target': 'local:validate'}, {'local.py': 'def validate(): pass'})
        self.assertEqual(report.status, 'PASS')

    def test_parameter_shadows_import(self):
        report = check('from local import validate\ndef main(validate): validate()', extra={'local.py': 'def validate(): pass'})
        self.assertNotEqual(report.status, 'PASS')

    def test_assignment_shadows_global_before_first_assignment(self):
        report = check('def validate(): pass\ndef main():\n    validate()\n    validate = 1')
        self.assertNotEqual(report.status, 'PASS')

    def test_reassignment_and_known_alias(self):
        base = 'def validate(): pass\ndef other(): pass\ndef main():\n    alias = validate\n'
        self.assertEqual(check(base + '    alias()').status, 'PASS')
        self.assertEqual(check(base + '    alias = other\n    alias()').status, 'FAIL')

    def test_test_only_call_and_disconnected_cycle(self):
        report = check('def validate(): cycle()\ndef cycle(): validate()\ndef main(): pass', extra={'tests/test_x.py': 'from app import validate\nvalidate()'})
        self.assertEqual(report.status, 'FAIL')
        self.assertNotIn('tests/test_x.py', report.scope)

    def test_comments_imports_and_unrelated_unsupported_are_not_wiring(self):
        self.assertEqual(check('def validate(): pass\ndef unrelated(x): return getattr(x,"go")()\ndef main():\n    # validate()\n    pass').status, 'FAIL')
        self.assertEqual(check('def validate(): pass\ndef unrelated(x): return getattr(x,"go")()\ndef main(): validate()').status, 'PASS')

    def test_nested_function_with_lexical_alias(self):
        self.assertEqual(check('def validate(): pass\ndef main():\n    alias = validate\n    def inner(): alias()\n    inner()').status, 'PASS')

    def test_opaque_decorator_and_reflection_are_unresolved(self):
        self.assertEqual(check('def validate(): pass\n@unknown\ndef main(): validate()').status, 'UNRESOLVED')
        self.assertEqual(check('def validate(): pass\ndef main(): getattr(object(),"validate")()').status, 'UNRESOLVED')

    def test_branch_mapping_storage_is_independent(self):
        source = 'def publish(flag):\n    data = {}\n    if flag: data["name"] = "County"\n    else: data["count"] = 24\n    return data'
        self.assertEqual(check(source, shape()).status, 'FAIL')

    def test_import_time_side_effects_not_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'effect'
            source = f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\nraise SystemExit()\ndef validate(): pass\ndef main(): validate()'
            self.assertEqual(check(source).status, 'PASS')
            self.assertFalse(marker.exists())


class MethodTests(unittest.TestCase):
    def run_method(self, source, target='app:Base.validate'):
        return check(source, {'kind': 'connection', 'id': 'c', 'entry': 'app:main', 'target': target})

    def test_inherited_method_and_same_named_other(self):
        source = 'class Base:\n    def validate(self): pass\nclass Child(Base): pass\nclass Other:\n    def validate(self): pass\ndef main(): Child().validate()'
        self.assertEqual(self.run_method(source).status, 'PASS')
        self.assertEqual(self.run_method(source.replace('Child().validate()', 'Other().validate()')).status, 'FAIL')

    def test_override_and_super(self):
        prefix = 'class Base:\n    def validate(self): pass\nclass Child(Base):\n    def validate(self): '
        suffix = '\ndef main(): Child().validate()'
        self.assertEqual(self.run_method(prefix + 'pass' + suffix).status, 'FAIL')
        self.assertEqual(self.run_method(prefix + 'super().validate()' + suffix).status, 'PASS')

    def test_c3_mro_diamond_and_cooperative_super(self):
        source = 'class Base:\n    def validate(self): pass\nclass Left(Base): pass\nclass Right(Base):\n    def validate(self): pass\nclass Child(Left,Right): pass\ndef main(): Child().validate()'
        self.assertEqual(self.run_method(source).status, 'FAIL')
        self.assertEqual(self.run_method(source.replace('class Right(Base):\n    def validate(self): pass', 'class Right(Base):\n    def validate(self): super().validate()')).status, 'PASS')

    def test_constructor_stored_receiver_alias(self):
        source = 'class Base:\n    def validate(self): pass\nclass Owner:\n    def __init__(self): self.service = Base()\n    def run(self): self.service.validate()\ndef main():\n    x = Owner()\n    y = x\n    y.run()'
        self.assertEqual(self.run_method(source).status, 'PASS')

    def test_static_class_and_property_bindings(self):
        for declaration, call in [('staticmethod\n    def validate(): pass', 'Base.validate()'), ('classmethod\n    def validate(cls): pass', 'Base.validate()'), ('property\n    def validate(self): return 3', 'Base().validate')]:
            with self.subTest(declaration=declaration):
                self.assertEqual(self.run_method('class Base:\n    @' + declaration + '\ndef main(): ' + call).status, 'PASS')

    def test_annotation_only_and_ambiguous_receiver_unresolved(self):
        source = 'class Base:\n    def validate(self): pass\ndef main(x: Base): x.validate()'
        self.assertEqual(self.run_method(source).status, 'UNRESOLVED')
        source = 'class Base:\n    def validate(self): pass\nclass Other:\n    def validate(self): pass\ndef main(flag):\n    x = Base() if flag else Other()\n    x.validate()'
        self.assertEqual(self.run_method(source).status, 'UNRESOLVED')

    def test_async_requires_await_or_supported_schedule(self):
        prefix = 'def validate(): pass\nasync def worker(): validate()\n'
        self.assertEqual(check(prefix + 'def main(): worker()').status, 'FAIL')
        self.assertEqual(check(prefix + 'async def main(): await worker()').status, 'PASS')
        self.assertEqual(check('import asyncio\n' + prefix + 'def main(): asyncio.run(worker())').status, 'PASS')


class HandoffTests(unittest.TestCase):
    prefix = 'def produce(): return {"value": 7}\ndef consume(payload): pass\ndef other(payload): pass\n'

    def test_identity_assignment_and_field(self):
        self.assertEqual(check(self.prefix + 'def main():\n    x = produce()\n    alias = x\n    consume(alias)', handoff()).status, 'PASS')
        self.assertEqual(check(self.prefix + 'def main():\n    x = produce()\n    consume(x["value"])', handoff(field='value')).status, 'PASS')
        self.assertEqual(check(self.prefix + 'def main():\n    x = produce()\n    consume(x.get("value"))', handoff(field='value')).status, 'PASS')

    def test_discard_overwrite_unrelated_field_wrong_argument(self):
        bodies = ['produce(); consume({"value": 7})', 'x = produce(); x = {}; consume(x)', 'x = produce(); y = {"value": 7}; consume(y["value"])', 'x = produce(); other(x)']
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(check(self.prefix + 'def main(): ' + body, handoff()).status, 'FAIL')
        source = 'def produce(): return 7\ndef consume(other, payload): pass\ndef main(): consume(produce(), 0)'
        self.assertEqual(check(source, handoff()).status, 'FAIL')

    def test_branch_missing_handoff_is_not_complete(self):
        source = self.prefix + 'def main(flag):\n    x = produce()\n    if flag:\n        consume(x)\n    else:\n        pass'
        self.assertEqual(check(source, handoff()).status, 'FAIL')

    def test_mutually_exclusive_producers_each_reach_consumer(self):
        source = self.prefix + 'def main(flag):\n    if flag:\n        x = produce()\n        consume(x)\n    else:\n        x = produce()\n        consume(x)'
        report = check(source, handoff())
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(len([edge for edge in report.contracts[0].witness if edge.kind == 'handoff']), 2)

    def test_helper_returns_preserve_branch_producer_correlation(self):
        for returned in ('x', 'x["value"]'):
            source = self.prefix + f'def wrapper(flag):\n    if flag:\n        x = produce()\n        return {returned}\n    x = produce()\n    return {returned}\ndef main(flag): consume(wrapper(flag))'
            with self.subTest(returned=returned):
                self.assertEqual(check(source, handoff(**({'field': 'value'} if returned != 'x' else {}))).status, 'PASS')
        source = self.prefix + 'def wrapper(flag): return produce() if flag else produce()\ndef main(flag): consume(wrapper(flag))'
        self.assertEqual(check(source, handoff()).status, 'PASS')

    def test_opaque_branch_keeps_missing_delivery_unresolved(self):
        source = self.prefix + '@opaque\ndef hidden(payload): pass\ndef main(flag):\n    x = produce()\n    if flag: consume(x)\n    else: hidden(x)'
        self.assertEqual(check(source, handoff()).status, 'UNRESOLVED')
        # A separate supported path still supplies a definite counterexample.
        source = source.replace('else: hidden(x)', 'elif unknown: hidden(x)\n    else: pass')
        self.assertEqual(check(source, handoff()).status, 'FAIL')

    def test_helper_branch_cannot_deliver_another_branchs_discarded_value(self):
        source = self.prefix + 'def wrapper(flag):\n    x = produce()\n    if flag: return x\n    return {"value": 7}\ndef main(flag): consume(wrapper(flag))'
        self.assertEqual(check(source, handoff()).status, 'FAIL')

    def test_returned_payload_preserves_branch_producer_correlation(self):
        contract = handoff(); contract.pop('consumer'); contract.pop('argument'); contract['returned_field'] = 'published'
        source = self.prefix + 'def wrapper(flag):\n    if flag: return produce()\n    return produce()\ndef main(flag): return {"published": wrapper(flag)}'
        self.assertEqual(check(source, contract).status, 'PASS')

    def test_wrapper_branch_cannot_claim_both_return_values_come_from_producer(self):
        source = self.prefix + 'def wrapper(flag):\n    if flag: return produce()\n    return {}\ndef main(flag): consume(wrapper(flag))'
        self.assertEqual(check(source, handoff()).status, 'FAIL')

    def test_ambiguous_callable_cannot_union_a_handoff(self):
        source = self.prefix + 'def main(flag):\n    dispatch = consume if flag else other\n    dispatch(produce())'
        self.assertEqual(check(source, handoff()).status, 'UNRESOLVED')

    def test_returned_payload_field(self):
        contract = handoff(); contract.pop('consumer'); contract.pop('argument'); contract['returned_field'] = 'published'
        self.assertEqual(check(self.prefix + 'def main(): return {"published": produce()}', contract).status, 'PASS')
        self.assertEqual(check(self.prefix + 'def main():\n    produce()\n    return {"published": 7}', contract).status, 'FAIL')

    def test_receiver_identity_and_reassignment(self):
        source = 'class Box:\n    def __init__(self, value): self.value = value\ndef produce(): return 7\ndef consume(payload): pass\ndef main():\n    x = Box(produce())\n    y = Box(7)\n    consume(x.value)'
        self.assertEqual(check(source, handoff()).status, 'PASS')
        self.assertEqual(check(source.replace('consume(x.value)', 'consume(y.value)'), handoff()).status, 'FAIL')


class FrameworkTests(unittest.TestCase):
    def test_prefect_result_delivery_ordering_and_bypass(self):
        prefix = 'from prefect import task as t, flow as f\n@t\ndef produce(): return 7\n@t\ndef consume(payload=0): pass\n@f\ndef main():\n    x = produce.submit()\n'
        self.assertEqual(check(prefix + '    consume.submit(x)', handoff()).status, 'PASS')
        self.assertEqual(check(prefix + '    consume.submit(x.result())', handoff()).status, 'PASS')
        self.assertEqual(check(prefix + '    consume.submit(wait_for=[x])', handoff()).status, 'FAIL')
        self.assertEqual(check(prefix + '    x.wait()\n    consume.submit(0)', handoff()).status, 'FAIL')
        self.assertEqual(check(prefix + '    consume.fn(x.result())', handoff()).status, 'PASS')
        self.assertEqual(check(prefix.replace('produce.submit()', 'produce.with_options(name="p").submit()') + '    consume.submit(x)', handoff()).status, 'PASS')
        self.assertEqual(check(prefix.replace('produce.submit()', 'produce.map([1,2])') + '    consume.submit(x)', handoff()).status, 'PASS')

    def test_typer_attached_child_and_callback_replacement(self):
        source = 'import typer\napp = typer.Typer()\nchild = typer.Typer()\ndef validate(): pass\n@child.command()\ndef command(): validate()\napp.add_typer(child)\ndef main(): app()'
        self.assertEqual(check(source).status, 'PASS')
        self.assertEqual(check(source.replace('app.add_typer(child)', '')).status, 'FAIL')
        source = 'import typer\ndef validate(): pass\ndef old(): validate()\napp = typer.Typer(callback=old)\n@app.callback()\ndef new(): pass\ndef main(): app()'
        self.assertEqual(check(source).status, 'FAIL')


class ShapeTests(unittest.TestCase):
    def test_literal_complete_and_branch_specific_missing(self):
        self.assertEqual(check('def publish(): return {"name": "County", "count": 24}', shape()).status, 'PASS')
        self.assertEqual(check('def publish(flag):\n    if flag: return {"name":"County"}\n    return {"count":24}', shape()).status, 'FAIL')

    def test_missing_extra_rename_nullable_types(self):
        for value in ['{"name":"County"}', '{"name":"County","count":24,"extra":0}', '{"name":"County","population":24}', '{"name":"County","count":None}', '{"name":"County","count":True}']:
            with self.subTest(value=value):
                self.assertEqual(check('def publish(): return ' + value, shape()).status, 'FAIL')
        nullable = shape(fields={'name': {'type': 'string'}, 'count': {'type': 'integer', 'nullable': True}})
        self.assertEqual(check('def publish(): return {"name":"County","count":None}', nullable).status, 'PASS')

    def test_pydantic_inheritance_alias_and_projection(self):
        source = 'from pydantic import BaseModel, Field\nclass Parent(BaseModel):\n    name: str\nclass Model(Parent):\n    count: int = Field(serialization_alias="residents")\ndef publish(): return Model(name="County", count=24).model_dump(by_alias=True)'
        contract = shape(fields={'name': {'type': 'string'}, 'residents': {'type': 'integer'}})
        self.assertEqual(check(source, contract).status, 'PASS')
        self.assertEqual(check(source.replace('by_alias=True', 'include={"name"}'), contract).status, 'FAIL')

    def test_pydantic_nullable_without_default_still_required(self):
        source = 'from pydantic import BaseModel\nclass Model(BaseModel):\n    name: str\n    count: int | None'
        self.assertEqual(check(source, shape(source='app:Model', mode='model', fields={'name': {'type':'string'}, 'count': {'type':'integer','nullable':True}})).status, 'PASS')
        self.assertEqual(check(source, shape(source='app:Model', mode='model', fields={'name': {'type':'string'}, 'count': {'type':'integer','nullable':True,'required':False}})).status, 'FAIL')

    def test_custom_serialization_and_conditional_projection_unresolved(self):
        prefix = 'from pydantic import BaseModel, model_serializer\nclass Model(BaseModel):\n    name: str\n    count: int\n'
        custom = prefix + '    @model_serializer\n    def serialize(self): return {}\ndef publish(): return Model(name="County", count=24).model_dump()'
        self.assertEqual(check(custom, shape()).status, 'UNRESOLVED')
        conditional = prefix + 'def publish(flag): return Model(name="County", count=24).model_dump(exclude_none=flag)'
        self.assertEqual(check(conditional, shape()).status, 'UNRESOLVED')

    def test_literal_exports_and_json_artifact(self):
        contract = shape(source='app:__all__', mode='exports', fields={'run': {'type':'string'}})
        self.assertEqual(check('__all__ = ["run"]', contract).status, 'PASS')
        self.assertEqual(check('__all__ = ["other"]', contract).status, 'FAIL')
        self.assertEqual(check('', shape(source='snapshot.json', mode='artifact'), {'snapshot.json':'{"name":"County","count":24}'}).status, 'PASS')


class Boundaries(unittest.TestCase):
    def test_deterministic_reports_and_limits(self):
        source = 'def validate(): pass\ndef main(): validate()'
        self.assertEqual(check(source).model_dump_json(), check(source).model_dump_json())
        r = analyze_sources({'app.py':source}, {'max_nodes':1,'contracts':[{'kind':'connection','id':'c','entry':'app:main','target':'app:validate'}]})
        self.assertEqual(r.status,'UNRESOLVED')

    def test_error_hashes_share_the_normalized_config_boundary(self):
        from sentinels.models import validate_config
        config = {'contracts': [{'kind': 'connection', 'id': 'c', 'entry': 'app:main', 'target': 'app:validate'}]}
        native = analyze_sources({'app.py': 'def broken('}, config)
        browser = analyze_sources({'app.py': 'def broken('}, validate_config(config))
        self.assertEqual(native.config_hash, browser.config_hash)
        self.assertEqual(native.status, 'ERROR')

    def test_syntax_duplicate_module_and_empty_scope_errors(self):
        self.assertEqual(check('def invalid(').status,'ERROR')
        self.assertEqual(check('', extra={'app/__init__.py':''}).status,'ERROR')
        self.assertEqual(analyze_sources({}, {'contracts':[{'kind':'connection','id':'c','entry':'app:main','target':'app:validate'}]}).status,'ERROR')

    def test_project_rejects_escaping_input_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'app.py').symlink_to('/etc/passwd')
            self.assertEqual(analyze_project(root, {'contracts':[{'kind':'connection','id':'c','entry':'app:main','target':'app:validate'}]}).status,'ERROR')


if __name__ == '__main__': unittest.main()
