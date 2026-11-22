import ast
import unittest
from runtime_body import runtime_body_lines

class RuntimeBodyTests(unittest.TestCase):
    def check(self, old, new, global_lines=()):
        return runtime_body_lines(ast.parse(old), ast.parse(new), set(global_lines))

    def test_excludes_imported_definition_but_keeps_entire_body(self):
        old='def f(flag):\n    if flag:\n        return 1\n    return 2\n'
        self.assertEqual(self.check(old,old.replace('return 1','return 3')), {2,3,4})

    def test_defaults_decorators_and_mixed_edits_keep_existing_dependencies(self):
        for old,new in [('def f(x=1):\n    return x\n','def f(x=2):\n    return x\n'),
                        ('@decorate\ndef f():\n    return 1\n','@decorate\ndef f():\n    return 2\n'),
                        ('X=1\ndef f():\n    return 1\n','X=2\ndef f():\n    return 2\n'),
                        ('class A:\n    X=1\n','class A:\n    X=2\n')]:
            self.assertIsNone(self.check(old,new))

    def test_import_or_collection_execution_declines(self):
        old='def f():\n    return 1\n'
        self.assertIsNone(self.check(old,old.replace('1','2'),[2]))
        self.assertEqual(self.check(old,old.replace('1','2'),[1]),{2})

    def test_inline_and_added_definition_decline(self):
        self.assertIsNone(self.check('def f(): return 1\n','def f(): return 2\n'))
        self.assertIsNone(self.check('x=1\n','x=1\ndef f():\n    return 2\n'))

    def test_callable_metadata_changes_decline(self):
        self.assertIsNone(self.check('def f():\n    "old doc"\n    return 1\n',
                                     'def f():\n    "new doc"\n    return 1\n'))
        self.assertIsNone(self.check('def f():\n    return 1\n',
                                     'def f():\n    yield 1\n'))
