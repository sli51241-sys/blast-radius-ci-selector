"""Synthetic offline graph checks; no event samples or heldout labels."""
import ast
import hashlib
from pathlib import Path
import tempfile
import unittest

from tools.cache_dependency_eval import CacheGraph, EnumIdentity, Node, UNKNOWN, _same


class CacheDependencyTests(unittest.TestCase):
    def graph(self, text, sites=()):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name)
        path = "pkg/core.py"
        (source / "pkg").mkdir()
        (source / path).write_text(text)
        return CacheGraph(source, {
            "source_sha256": {path: hashlib.sha256(text.encode()).hexdigest()},
            "sites": {path: {"A": {"FLAG": [{"path": path, "line": line} for line in sites]}}},
        })

    def test_transitive_module_alias_cache_is_reached(self):
        graph = self.graph("class A:\n    FLAG = True\nCACHED = A.FLAG\nALIAS = CACHED\n", [3])
        seed = Node("pkg/core.py", "A", "FLAG")
        reached, _, blockers = graph.propagate({seed: True}, {seed: False})
        self.assertEqual(reached, {seed, Node("pkg/core.py", "", "CACHED"), Node("pkg/core.py", "", "ALIAS")})
        self.assertEqual(blockers, set())

    def test_bare_class_cache_dependency_is_reached(self):
        graph = self.graph("class A:\n    FLAG = True\n    CACHED = FLAG\n")
        seed = Node("pkg/core.py", "A", "FLAG")
        reached, _, blockers = graph.propagate({seed: True}, {seed: False})
        self.assertIn(Node("pkg/core.py", "A", "CACHED"), reached)
        self.assertFalse(blockers)

    def test_if_control_dependency_tracks_both_possible_values(self):
        graph = self.graph("class A:\n    FLAG = True\nif A.FLAG:\n    CACHED = 1\nelse:\n    CACHED = 0\n", [3])
        seed, cached = Node("pkg/core.py", "A", "FLAG"), Node("pkg/core.py", "", "CACHED")
        self.assertEqual(graph.value(cached, {seed: True}), 1)
        self.assertEqual(graph.value(cached, {seed: False}), 0)
        reached, _, blockers = graph.propagate({seed: True}, {seed: False})
        self.assertIn(cached, reached)
        self.assertFalse(blockers)

    def test_exact_unchanged_derived_value_stops_propagation(self):
        graph = self.graph("class A:\n    FLAG = 1\nCACHED = bool(A.FLAG)\n", [3])
        seed, cached = Node("pkg/core.py", "A", "FLAG"), Node("pkg/core.py", "", "CACHED")
        reached, unchanged, blockers = graph.propagate({seed: 1}, {seed: 2})
        self.assertNotIn(cached, reached)
        self.assertIn(cached, unchanged)
        self.assertFalse(blockers)

    def test_metaclass_function_cache_is_explicitly_unsupported(self):
        graph = self.graph("class A:\n    FLAG = True\ndef derive(cls):\n    return cls.FLAG\n", [4])
        seed = Node("pkg/core.py", "A", "FLAG")
        _, _, blockers = graph.propagate({seed: True}, {seed: False})
        self.assertIn("function/metaclass cache read unsupported", blockers)

    def test_unknown_call_or_container_forces_fallback(self):
        for expression in ("unknown(A.FLAG)", "{'flag': A.FLAG}"):
            with self.subTest(expression=expression):
                graph = self.graph("class A:\n    FLAG = True\nCACHED = " + expression + "\n", [3])
                seed = Node("pkg/core.py", "A", "FLAG")
                _, _, blockers = graph.propagate({seed: True}, {seed: False})
                self.assertTrue(blockers)

    def test_module_and_class_shadowed_conversions_are_unknown(self):
        for text in ("bool = other\nclass A:\n    FLAG = True\n    CACHED = bool(FLAG)\n",
                     "class A:\n    bool = other\n    FLAG = True\n    CACHED = bool(FLAG)\n"):
            with self.subTest(text=text):
                graph = self.graph(text)
                self.assertIs(graph.value(Node("pkg/core.py", "A", "CACHED"), {}), UNKNOWN)

    def test_primitive_types_are_exact_and_bool_does_not_equal_int(self):
        graph = self.graph("class A:\n    FLAG = None\n")
        for expression, expected in (("None", None), ("False", False), ("'x'", "x"), ("-7", -7), ("int('7')", 7)):
            with self.subTest(expression=expression):
                value = graph.expression("pkg/core.py", "A", ast.parse(expression, mode="eval").body, {})
                self.assertTrue(_same(value, expected))
        self.assertFalse(_same(True, 1))

    def test_enum_identity_is_source_proven_without_guessing_numeric_value(self):
        graph = self.graph("from enum import Enum\nclass Mode(Enum):\n    FIRST = 1\n    SECOND = 2\nclass A:\n    FLAG = Mode.FIRST\n")
        value = graph.value(Node("pkg/core.py", "A", "FLAG"), {})
        self.assertEqual(value, EnumIdentity("pkg/core.py", "Mode", "FIRST"))
        expression = ast.parse("bool(Mode.FIRST)", mode="eval").body
        self.assertIs(graph.expression("pkg/core.py", "A", expression, {}), UNKNOWN)

    def test_unsupported_loop_read_is_not_silently_certified(self):
        graph = self.graph("class A:\n    FLAG = True\nfor item in [1]:\n    CACHED = A.FLAG\n", [4])
        seed = Node("pkg/core.py", "A", "FLAG")
        _, _, blockers = graph.propagate({seed: True}, {seed: False})
        self.assertIn("read not covered by direct assignment graph", blockers)

    def test_bare_name_in_loop_or_external_write_is_not_lost(self):
        for statement in ("for item in [1]:\n        CACHED = FLAG", "other.CACHED = FLAG", "unknown(FLAG)"):
            with self.subTest(statement=statement):
                graph = self.graph("class A:\n    FLAG = True\n    " + statement + "\n")
                seed = Node("pkg/core.py", "A", "FLAG")
                _, _, blockers = graph.propagate({seed: True}, {seed: False})
                self.assertTrue(blockers)

    def test_bare_name_in_function_default_forces_fallback(self):
        graph = self.graph("class A:\n    FLAG = True\n    def method(value=FLAG):\n        return value\n")
        seed = Node("pkg/core.py", "A", "FLAG")
        _, _, blockers = graph.propagate({seed: True}, {seed: False})
        self.assertIn("function default/annotation dependency unsupported", blockers)


if __name__ == "__main__":
    unittest.main()
