"""Class analysis must include every Python statement suite."""
import ast
import unittest
from class_dependencies import class_bindings, generated_classes, statement_classes
from selector import Selector

SOURCE = '''
class Outer:
    DIRECT = {'a': [1, 2, 3]}
    if condition:
        CONDITIONAL = 2
    def method(self):
        local = 4
        for value in values:
            class InFor:
                FIELD = 1
        else:
            class ForElse:
                FIELD = 2
        while condition:
            class InWhile:
                FIELD = 3
        with context():
            class InWith:
                FIELD = 4
        try:
            class InTry:
                FIELD = 5
        except Exception:
            class InExcept:
                FIELD = 6
        else:
            class InElse:
                FIELD = 7
        finally:
            class InFinally:
                FIELD = 8
        try:
            operation()
        except* Exception:
            class InExceptionGroup:
                FIELD = 9
        match value:
            case _:
                class InMatch:
                    FIELD = 10
async def outer():
    async with context():
        async for value in values:
            class InAsync:
                FIELD = 11
'''

class StatementTraversalTests(unittest.TestCase):
    def test_all_statement_suites(self):
        tree = ast.parse(SOURCE)
        expected = {node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        self.assertEqual(set(statement_classes(tree)), expected)
        self.assertEqual(len(expected), 12)

    def test_binding_scopes_and_local_qualification(self):
        found = {(b.qualname, b.attribute) for b in class_bindings(ast.parse(SOURCE))}
        expected = {('Outer', 'DIRECT'), ('Outer', 'CONDITIONAL')}
        expected.update((f'Outer.method.<locals>.{name}', 'FIELD') for name in (
            'InFor', 'ForElse', 'InWhile', 'InWith', 'InTry', 'InExcept', 'InElse',
            'InFinally', 'InExceptionGroup', 'InMatch'))
        expected.add(('outer.<locals>.InAsync', 'FIELD'))
        self.assertEqual(found, expected)

    def test_changed_assignment_coordinates(self):
        tree = ast.parse(SOURCE)
        assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
        for node in assignments:
            if any(isinstance(t, ast.Name) and t.id == 'FIELD' for t in node.targets):
                self.assertEqual(Selector.changed_class_data(tree, {node.lineno}), {'FIELD'})
        line = next(n.lineno for n in assignments if any(isinstance(t, ast.Name) and t.id == 'CONDITIONAL' for t in n.targets))
        # This helper keeps its historical immediate-class-body scope.
        self.assertEqual(Selector.changed_class_data(tree, {line}), set())

    def test_generated_local_classes(self):
        for source in ('def factory():\n    @decorate\n    class Local:\n        FLAG = 1\n',
                       'try:\n    operation()\nexcept* Exception:\n    class Local(Enum):\n        FLAG = 1\n'):
            self.assertTrue(generated_classes(ast.parse(source)))
        self.assertFalse(generated_classes(ast.parse(SOURCE)))

if __name__ == '__main__':
    unittest.main()
