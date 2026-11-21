"""Synthetic projection proofs and conservative refusal cases; no event data."""
import ast
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from class_projection import PINNED_COMMIT, dict_values_import_safe


class ProjectionTests(unittest.TestCase):
    old = "class A:\n    FIELD = {'x': (1, 2), 'y': (3, 4)}\n"
    new = "class A:\n    FIELD = {'x': (9, 2), 'y': (3, 4)}\n"

    def check(self, reader, *, old=None, new=None, dictionary_reader=None, mutate=None):
        old, new = old or self.old, new or self.new
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            source = Path(temporary)
            target = "pkg/core.py"
            files = {target: old, "pkg/reader.py": reader}
            for relative, text in files.items():
                file = source / relative
                file.parent.mkdir(exist_ok=True)
                file.write_text(text)
            old_tree, new_tree = ast.parse(old), ast.parse(new)
            old_value = next(node.value for node in ast.walk(old_tree)
                             if isinstance(node, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == "FIELD" for t in node.targets))
            new_value = next(node.value for node in ast.walk(new_tree)
                             if isinstance(node, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == "FIELD" for t in node.targets))
            sites = {"FIELD": [{"path": "pkg/reader.py", "line": 2}]}
            if dictionary_reader:
                (source / "pkg/dictionary.py").write_text(dictionary_reader)
                files["pkg/dictionary.py"] = dictionary_reader
                sites["__dict__"] = [{"path": "pkg/dictionary.py", "line": 2}]
            metadata = {
                "version": 1, "complete": True, "collection_complete": True, "pinned_commit": PINNED_COMMIT,
                "source_sha256": {path: hashlib.sha256(text.encode()).hexdigest()
                                  for path, text in files.items()},
                "sites": {target: {"A": sites}},
            }
            if mutate:
                metadata = mutate(copy.deepcopy(metadata))
            return dict_values_import_safe(
                source=source, path=target, owner="A", attribute="FIELD",
                old_value=old_value, new_value=new_value, old_tree=old_tree,
                metadata=metadata, changed_paths={target},
            )

    def test_direct_membership_and_keys_membership_allow_value_edit(self):
        for expression in ("'x' in cls.FIELD", "'x' not in cls.FIELD", "'x' in cls.FIELD.keys()"):
            with self.subTest(expression=expression):
                self.assertTrue(self.check(f"def derive(cls):\n    return {expression}\n"))

    def test_single_local_alias_used_only_for_membership_is_safe(self):
        self.assertTrue(self.check(
            "def derive(cls):\n    values = cls.FIELD\n    if 'x' in values:\n        return 'y' not in values\n    return False\n"
        ))

    def test_unrecognized_value_consumers_are_rejected(self):
        for expression in ("unknown(cls.FIELD)", "cls.FIELD.get('x')", "cls.FIELD.items()",
                           "cls.FIELD.values()", "cls.FIELD['x']", "cls.FIELD",
                           "'x' in cls.FIELD.keys().mapping", "tuple(cls.FIELD.keys())"):
            with self.subTest(expression=expression):
                self.assertFalse(self.check(f"def derive(cls):\n    return {expression}\n"))

    def test_alias_escapes_and_reassignments_are_rejected(self):
        tails = ("return unknown(values)", "return values.get('x')", "values = {}\n    return 'x' in values",
                 "values += {}\n    return 'x' in values", "del values\n    return False",
                 "def later():\n        return 'x' in values\n    return later")
        for tail in tails:
            with self.subTest(tail=tail):
                self.assertFalse(self.check("def derive(cls):\n    values = cls.FIELD\n    " + tail + "\n"))

    def test_nonassignment_bindings_also_invalidate_the_alias(self):
        tails = ("try:\n        effect()\n    except Error as values:\n        pass\n    return 'x' in values",
                 "import other as values\n    return 'x' in values",
                 "match other:\n        case values:\n            pass\n    return 'x' in values")
        for tail in tails:
            with self.subTest(tail=tail):
                self.assertFalse(self.check("def derive(cls):\n    values = cls.FIELD\n    " + tail + "\n"))

    def test_module_or_class_alias_can_escape_and_is_rejected(self):
        for reader in ("# module\nvalues = A.FIELD\nresult = 'x' in values\n",
                       "class B:\n    values = A.FIELD\n    result = 'x' in values\n"):
            with self.subTest(reader=reader):
                self.assertFalse(self.check(reader))

    def test_missing_incomplete_or_stale_metadata_is_rejected(self):
        mutations = (
            lambda m: None,
            lambda m: {**m, "complete": False},
            lambda m: {**m, "collection_complete": False},
            lambda m: {key: value for key, value in m.items() if key != "collection_complete"},
            lambda m: {**m, "pinned_commit": "other"},
            lambda m: {**m, "sites": {}},
            lambda m: {**m, "source_sha256": {}},
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.assertFalse(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n", mutate=mutate))

    def test_missing_field_site_is_rejected(self):
        def remove(m):
            m["sites"]["pkg/core.py"]["A"].pop("FIELD")
            return m
        self.assertFalse(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n", mutate=remove))

    def test_changed_keys_order_or_types_are_rejected(self):
        for value in ("{'z': (9, 2), 'y': (3, 4)}", "{'y': (3, 4), 'x': (9, 2)}",
                      "{'x': (9, 2)}", "{'x': (9, 2), 'y': (3, 4), 'z': 5}"):
            with self.subTest(value=value):
                self.assertFalse(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n",
                                            new=f"class A:\n    FIELD = {value}\n"))
        self.assertFalse(self.check("def derive(cls):\n    return 1 in cls.FIELD\n",
                                    old="class A:\n    FIELD = {1: 2}\n",
                                    new="class A:\n    FIELD = {True: 3}\n"))

    def test_nonliteral_dictionary_values_are_rejected(self):
        self.assertFalse(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n",
                                    new="class A:\n    FIELD = {'x': effect(), 'y': (3, 4)}\n"))

    def test_direct_bare_class_body_dependency_is_rejected(self):
        old = self.old + "    CACHED = FIELD\n"
        new = self.new + "    CACHED = FIELD\n"
        self.assertFalse(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n", old=old, new=new))

    def test_method_default_bare_dependency_is_rejected(self):
        suffix = "    def method(self, cached=FIELD):\n        return cached\n"
        self.assertFalse(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n",
                                    old=self.old + suffix, new=self.new + suffix))

    def test_ordinary_method_body_does_not_create_import_cache(self):
        suffix = "    def method(self):\n        return FIELD\n"
        self.assertTrue(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n",
                                   old=self.old + suffix, new=self.new + suffix))

    def test_local_class_bare_dependency_is_rejected(self):
        old = "def factory():\n    class A:\n        FIELD = {'x': 1}\n        CACHED = FIELD\n    return A\n"
        self.assertFalse(self.check("def derive(cls):\n    return 'x' in cls.FIELD\n",
                                    old=old, new=old.replace("'x': 1", "'x': 2")))

    def test_class_dictionary_membership_is_safe_but_value_reads_are_rejected(self):
        reader = "def derive(cls):\n    return 'x' in cls.FIELD\n"
        for expression in ("'OTHER' in cls.__dict__", "'FIELD' in cls.__dict__.keys()",
                           "'OTHER' in vars(cls)"):
            with self.subTest(expression=expression):
                self.assertTrue(self.check(reader, dictionary_reader=f"def cache(cls):\n    return {expression}\n"))
        for expression in ("cls.__dict__.get('OTHER')", "cls.__dict__['OTHER']",
                           "cls.__dict__.items()", "cls.__dict__.values()", "unknown(cls.__dict__)"):
            with self.subTest(expression=expression):
                self.assertFalse(self.check(reader, dictionary_reader=f"def cache(cls):\n    return {expression}\n"))

    def test_literal_getattr_membership_and_hasattr_are_safe(self):
        for expression in ("'x' in getattr(cls, 'FIELD')", "hasattr(cls, 'FIELD')"):
            with self.subTest(expression=expression):
                self.assertTrue(self.check(f"def derive(cls):\n    return {expression}\n"))

    def test_dynamic_getattr_site_is_rejected(self):
        self.assertFalse(self.check("def derive(cls):\n    return 'x' in getattr(cls, name)\n"))

    def test_second_value_read_site_invalidates_otherwise_safe_metadata(self):
        def append(m):
            m["sites"]["pkg/core.py"]["A"]["FIELD"].append({"path": "pkg/reader.py", "line": 3})
            return m
        self.assertFalse(self.check("def derive(cls):\n    safe = 'x' in cls.FIELD\n    return cls.FIELD['x']\n", mutate=append))


if __name__ == "__main__":
    unittest.main()
