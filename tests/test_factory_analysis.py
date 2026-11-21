import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factory_analysis import changed_factory_arguments


FACTORY = '''from __future__ import annotations
def make(*args, **kwargs):
    def callback(self: ForwardClass) -> Result:
        return self.execute(*args, **kwargs)
    return callback
'''

CALLER = '''from pkg.factories import make
class Config:
    TABLE = {**Base.TABLE, 'one': make('ONE', enabled=True), 'two': make('TWO')}
'''


class FactoryArgumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        (self.source / "pkg").mkdir()
        (self.source / "pkg/factories.py").write_text(FACTORY)

    def check(self, old, new, *, factory=FACTORY, overrides=None, owner="Config"):
        (self.source / "pkg/factories.py").write_text(factory)
        before, after = ast.parse(old), ast.parse(new)

        def binding(tree):
            cls = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.ClassDef) and node.name == owner.split(".")[-1])
            statement = next(node for node in cls.body
                             if isinstance(node, ast.Assign) and any(
                                 isinstance(target, ast.Name) and target.id == "TABLE"
                                 for target in node.targets))
            return SimpleNamespace(value=statement.value, kind=type(statement).__name__)

        bindings = {(owner, "TABLE"): ([binding(before)], [binding(after)])}
        return changed_factory_arguments(before, after, bindings, self.source, overrides)

    def edited(self, source=CALLER):
        return source.replace("enabled=True", "enabled=False")

    def test_imported_deferred_factory_accepts_literal_keyword_change(self):
        self.assertTrue(self.check(CALLER, self.edited()))

    def test_positional_literal_change_is_accepted(self):
        self.assertTrue(self.check(CALLER, CALLER.replace("'ONE'", "'FIRST'")))

    def test_nested_literal_argument_values_are_accepted(self):
        old = CALLER.replace("enabled=True", "options={'x': [1, -2]}")
        self.assertTrue(self.check(old, old.replace("[1, -2]", "[1, -3]")))

    def test_import_alias_is_resolved(self):
        old = CALLER.replace("import make", "import make as build").replace(" make(", " build(")
        self.assertTrue(self.check(old, self.edited(old)))

    def test_module_local_factory_is_accepted(self):
        old = FACTORY + CALLER.replace("from pkg.factories import make\n", "")
        self.assertTrue(self.check(old, self.edited(old)))

    def test_direct_return_lambda_factory_is_accepted(self):
        factory = "def make(*args, **kwargs):\n    return lambda self: self.execute(*args, **kwargs)\n"
        self.assertTrue(self.check(CALLER, self.edited(), factory=factory))

    def test_factory_docstring_is_accepted(self):
        factory = FACTORY.replace("def make(*args, **kwargs):", "def make(*args, **kwargs):\n    'Create a deferred callback.'")
        self.assertTrue(self.check(CALLER, self.edited(), factory=factory))

    def test_multiple_factory_arguments_can_change(self):
        self.assertTrue(self.check(CALLER, self.edited().replace("'TWO'", "'SECOND'")))

    def test_nested_data_container_is_accepted(self):
        old = CALLER.replace("'one': make('ONE', enabled=True)", "'one': [make('ONE', enabled=True)]")
        self.assertTrue(self.check(old, self.edited(old)))

    def test_unchanged_dictionary_keys_iteration_is_accepted(self):
        for statement in (
            "    KEYS = consume(key.split(' ') for key in TABLE)\n",
            "    KEYS = [key for key in TABLE]\n",
            "    KEYS = {key for key in TABLE}\n",
            "    KEYS = {key: 1 for key in TABLE}\n",
        ):
            with self.subTest(statement=statement):
                old = CALLER + statement
                self.assertTrue(self.check(old, self.edited(old)))

    def test_table_value_reads_during_class_creation_are_declined(self):
        for statement in (
            "    CACHED = TABLE['one'](receiver)\n",
            "    CACHED = TABLE['one']\n",
            "    ALIAS = TABLE\n",
            "    CACHED = consume(TABLE)\n",
            "    CACHED = TABLE.get('one')\n",
            "    CACHED = consume(TABLE.items())\n",
            "    CACHED = consume(TABLE.values())\n",
            "    CACHED = consume(TABLE.keys())\n",
            "    CACHED = [TABLE[key] for key in TABLE]\n",
        ):
            with self.subTest(statement=statement):
                old = CALLER + statement
                self.assertFalse(self.check(old, self.edited(old)))

    def test_table_reads_in_method_definition_headers_are_declined(self):
        for statement in (
            "    def method(self, callback=TABLE['one']):\n        return callback\n",
            "    @decorate(TABLE['one'])\n    def method(self):\n        return 1\n",
            "    def method(self, callback: TABLE['one']):\n        return callback\n",
            "    DEFERRED = lambda callback=TABLE['one']: callback\n",
        ):
            with self.subTest(statement=statement):
                old = CALLER + statement
                self.assertFalse(self.check(old, self.edited(old)))

    def test_table_reads_in_deferred_method_and_lambda_bodies_do_not_block(self):
        for statement in (
            "    def method(self):\n        return TABLE['one']\n",
            "    DEFERRED = lambda: TABLE['one']\n",
        ):
            with self.subTest(statement=statement):
                old = CALLER + statement
                self.assertTrue(self.check(old, self.edited(old)))

    def test_iteration_of_list_callbacks_is_not_dictionary_key_iteration(self):
        old = "from pkg.factories import make\nclass Config:\n    TABLE = [make('ONE', enabled=True)]\n    CACHED = consume(callback for callback in TABLE)\n"
        self.assertFalse(self.check(old, self.edited(old)))

    def test_changed_callee_is_declined(self):
        self.assertFalse(self.check(CALLER, self.edited().replace("make('ONE'", "other('ONE'")))

    def test_changed_keyword_name_is_declined(self):
        self.assertFalse(self.check(CALLER, CALLER.replace("enabled=True", "other=False")))

    def test_changed_argument_count_is_declined(self):
        self.assertFalse(self.check(CALLER, self.edited().replace("make('ONE',", "make('ONE', 2,")))

    def test_changed_table_key_is_declined(self):
        self.assertFalse(self.check(CALLER, self.edited().replace("'one':", "'other':")))

    def test_other_changed_table_value_is_declined(self):
        old = CALLER.replace("'two': make('TWO')", "'two': 1")
        self.assertFalse(self.check(old, self.edited(old).replace("'two': 1", "'two': 2")))

    def test_changed_class_header_is_declined(self):
        self.assertFalse(self.check(CALLER, self.edited().replace("class Config:", "class Config(Other):")))

    def test_unrelated_function_edit_is_declined(self):
        old = CALLER + "def unrelated():\n    return 1\n"
        self.assertFalse(self.check(old, self.edited(old).replace("return 1", "return 2")))

    def test_nonliteral_changed_argument_is_declined(self):
        for expression in ("effect()", "settings.FLAG", "flag", "set()"):
            with self.subTest(expression=expression):
                self.assertFalse(self.check(CALLER, CALLER.replace("enabled=True", "enabled=" + expression)))

    def test_starred_positional_arguments_are_declined(self):
        old = CALLER.replace("make('ONE',", "make(*OPTIONS,")
        self.assertFalse(self.check(old, self.edited(old)))

    def test_keyword_unpacking_is_declined(self):
        old = CALLER.replace("enabled=True)", "enabled=True, **OPTIONS)")
        self.assertFalse(self.check(old, self.edited(old)))

    def test_factory_call_passed_to_another_call_is_declined(self):
        old = CALLER.replace("make('ONE', enabled=True)", "register(make('ONE', enabled=True))")
        self.assertFalse(self.check(old, self.edited(old)))

    def test_factory_call_used_as_dict_key_is_declined(self):
        old = CALLER.replace("'one': make('ONE', enabled=True)", "make('ONE', enabled=True): 'one'")
        self.assertFalse(self.check(old, self.edited(old)))

    def test_factory_call_inside_comprehension_is_declined(self):
        old = CALLER.replace("make('ONE', enabled=True)", "[make('ONE', enabled=True) for x in values]")
        self.assertFalse(self.check(old, self.edited(old)))

    def test_factory_side_effect_statement_is_declined(self):
        factory = FACTORY.replace("    def callback", "    record(args)\n    def callback")
        self.assertFalse(self.check(CALLER, self.edited(), factory=factory))

    def test_factory_assignment_is_declined(self):
        factory = FACTORY.replace("    def callback", "    value = args[0]\n    def callback")
        self.assertFalse(self.check(CALLER, self.edited(), factory=factory))

    def test_factory_returning_noncallback_is_declined(self):
        self.assertFalse(self.check(CALLER, self.edited(), factory="def make(*args, **kwargs):\n    return 1\n"))

    def test_factory_decorator_is_declined(self):
        factory = FACTORY.replace("def make", "@decorate\ndef make")
        self.assertFalse(self.check(CALLER, self.edited(), factory=factory))

    def test_callback_decorator_is_declined(self):
        factory = FACTORY.replace("    def callback", "    @decorate\n    def callback")
        self.assertFalse(self.check(CALLER, self.edited(), factory=factory))

    def test_callback_defaults_are_declined(self):
        factory = FACTORY.replace("self: ForwardClass", "self: ForwardClass, value=effect()")
        self.assertFalse(self.check(CALLER, self.edited(), factory=factory))

    def test_eager_callback_annotations_are_declined(self):
        factory = FACTORY.replace("from __future__ import annotations\n", "")
        self.assertFalse(self.check(CALLER, self.edited(), factory=factory))

    def test_no_annotation_callback_is_accepted_without_future(self):
        factory = "def make(*args, **kwargs):\n    def callback(self):\n        return self.execute(*args, **kwargs)\n    return callback\n"
        self.assertTrue(self.check(CALLER, self.edited(), factory=factory))

    def test_modified_imported_factory_module_is_declined(self):
        self.assertFalse(self.check(CALLER, self.edited(), overrides={"pkg/factories.py": FACTORY}))

    def test_unrelated_modified_module_does_not_block(self):
        self.assertTrue(self.check(CALLER, self.edited(), overrides={"other.py": "value = 2\n"}))

    def test_module_rebinding_is_declined(self):
        for rebinding in ("make = other\n", "make += other\n", "del make\n"):
            with self.subTest(rebinding=rebinding):
                old = CALLER.replace("class Config:", rebinding + "class Config:")
                self.assertFalse(self.check(old, self.edited(old)))

    def test_rebound_factory_definition_in_imported_module_is_declined(self):
        self.assertFalse(self.check(CALLER, self.edited(), factory=FACTORY + "make = other\n"))

    def test_wildcard_imports_make_binding_resolution_uncertain(self):
        old = "from other import *\n" + CALLER
        self.assertFalse(self.check(old, self.edited(old)))
        self.assertFalse(self.check(CALLER, self.edited(), factory=FACTORY + "from other import *\n"))

    def test_class_binding_shadow_is_declined(self):
        for binding in ("    make = other\n", "    def make(self):\n        return other\n"):
            with self.subTest(binding=binding):
                old = CALLER.replace("    TABLE", binding + "    TABLE")
                self.assertFalse(self.check(old, self.edited(old)))

    def test_enclosing_class_shadow_is_declined(self):
        old = "from pkg.factories import make\nclass Outer:\n    make = other\n    class Config:\n        TABLE = {'one': make('ONE', enabled=True)}\n"
        self.assertFalse(self.check(old, self.edited(old), owner="Outer.Config"))

    def test_missing_source_and_reexport_are_declined(self):
        self.assertFalse(self.check(CALLER, self.edited(), factory="from other import make\n"))
        old = CALLER.replace("pkg.factories", "pkg.missing")
        self.assertFalse(self.check(old, self.edited(old)))

    def test_no_changed_literal_call_arguments_returns_before_resolution(self):
        old = "class Config:\n    TABLE = {'one': 1}\n"
        with patch("factory_analysis._resolve_factory", side_effect=AssertionError("should not resolve")):
            self.assertFalse(self.check(old, old.replace("1", "2")))
            self.assertFalse(self.check(CALLER, CALLER))

    def test_augmented_assignment_binding_is_declined(self):
        old = ast.parse(CALLER)
        new = ast.parse(self.edited())
        before = next(node for node in ast.walk(old) if isinstance(node, ast.ClassDef)).body[0]
        after = next(node for node in ast.walk(new) if isinstance(node, ast.ClassDef)).body[0]
        bindings = {("Config", "TABLE"): (
            [SimpleNamespace(value=before.value, kind="AugAssign")],
            [SimpleNamespace(value=after.value, kind="AugAssign")],
        )}
        self.assertFalse(changed_factory_arguments(old, new, bindings, self.source))

    def test_dunder_binding_is_declined(self):
        old = ast.parse(CALLER.replace("TABLE", "__call__"))
        new = ast.parse(self.edited().replace("TABLE", "__call__"))
        before = next(node for node in ast.walk(old) if isinstance(node, ast.ClassDef)).body[0]
        after = next(node for node in ast.walk(new) if isinstance(node, ast.ClassDef)).body[0]
        bindings = {("Config", "__call__"): (
            [SimpleNamespace(value=before.value, kind="Assign")],
            [SimpleNamespace(value=after.value, kind="Assign")],
        )}
        self.assertFalse(changed_factory_arguments(old, new, bindings, self.source))


if __name__ == "__main__":
    unittest.main()
