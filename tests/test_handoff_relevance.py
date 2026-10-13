"""Helper summaries retain causal flow without multiplying unrelated branches."""
import unittest

from sentinels import analyze_sources
from sentinels.models import Config


def check(source, **destination):
    config = {
        "version": Config.model_fields["version"].default,
        "contracts": [{"kind": "handoff", "id": "flow", "entry": "app:main",
                       "producer": "app:produce", "consumer": "app:consume",
                       "argument": "payload", **destination}],
    }
    return analyze_sources({"app.py": source}, config)


class HandoffRelevanceTests(unittest.TestCase):
    prefix = 'def produce(): return {"value": 7}\ndef consume(payload): pass\n'
    noise = ('def noise(flag):\n'
             '    if flag: a = 1\n'
             '    else: a = 2\n'
             '    if flag: b = 3\n'
             '    else: b = 4\n'
             '    return a, b\n')

    def test_unrelated_helpers_do_not_multiply_handoff_paths(self):
        source = self.prefix + self.noise + ('def main(flag):\n'
                 '    value = produce()\n'
                 '    a = noise(flag)\n'
                 '    b = noise(flag)\n'
                 '    c = noise(flag)\n'
                 '    d = noise(flag)\n'
                 '    consume(value)\n')
        report = check(source)
        self.assertEqual(report.status, 'PASS', report.model_dump())
        self.assertTrue(any(edge.kind == 'handoff' for edge in report.contracts[0].witness))
        for fault in ('value = {}', 'produce()', 'pass'):
            broken = source.replace('    consume(value)', f'    {fault}')
            self.assertEqual(check(broken).status, 'FAIL')

    def test_relevant_helper_alternatives_keep_identity_and_dispatch(self):
        cases = [
            ('def wrapper(flag):\n    x = produce()\n    if flag: return x\n    return {}\n'
             'def main(flag): consume(wrapper(flag))', 'FAIL'),
            ('class Box:\n    def __init__(self, value): self.value = value\n'
             'def wrapper(flag):\n    x = Box(produce())\n    if flag: return x\n    return Box(7)\n'
             'def main(flag): consume(wrapper(flag).value)', 'FAIL'),
            ('def other(payload): pass\n'
             'def wrapper(flag):\n    if flag: return consume\n    return other\n'
             'def main(flag): wrapper(flag)(produce())', 'FAIL'),
            ('@opaque\ndef hidden(payload): pass\n'
             'def wrapper(flag, payload):\n    if flag: consume(payload)\n    else: hidden(payload)\n'
             'def main(flag): wrapper(flag, produce())', 'UNRESOLVED'),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(check(self.prefix + body).status, expected)


if __name__ == '__main__':
    unittest.main()
