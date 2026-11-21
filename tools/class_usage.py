"""Observe qualified baseline class users without altering checkout files.

Only the offline coverage builder installs this loader. It adds observation
calls to method entries and selected attribute reads, preserving signatures,
source line numbers, descriptors, and the original getattr/hasattr callable.
No patch or labeled answer is read. The submitted selector never imports this.
"""

from __future__ import annotations

import ast
import builtins
import importlib.abc
import importlib.machinery
import sys
from collections import defaultdict
from pathlib import Path


RECEIVER = "__blast_usage_receiver__"
ATTRIBUTE = "__blast_usage_attribute__"
CALL = "__blast_usage_call__"


def _class_fields(tree):
    fields, classes = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        classes.add(node.name)
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign) and (
                statement.value is not None
                or any(isinstance(part, ast.Attribute) and part.attr == "ClassVar" for part in ast.walk(statement.annotation))
                or any(isinstance(part, ast.Name) and part.id == "ClassVar" for part in ast.walk(statement.annotation))
            ):
                targets = [statement.target]
            else:
                continue
            for target in targets:
                fields.update(part.id for part in ast.walk(target) if isinstance(part, ast.Name))
    return fields, classes


def _qualified_classes(tree):
    result = set()

    def walk(node, prefix=""):
        if isinstance(node, ast.ClassDef):
            name = prefix + node.name
            result.add(name)
            prefix = name + "."
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix += node.name + ".<locals>."
        for child in ast.iter_child_nodes(node):
            walk(child, prefix)

    walk(tree)
    return result


class _UsageTransformer(ast.NodeTransformer):
    def __init__(self, attribute_names):
        self.attribute_names = attribute_names
        self.class_depth = 0

    def visit_ClassDef(self, node):
        self.class_depth += 1
        node = self.generic_visit(node)
        self.class_depth -= 1
        return node

    def visit_FunctionDef(self, node):
        node = self.generic_visit(node)
        positional = node.args.posonlyargs + node.args.args
        if self.class_depth and positional and positional[0].arg in ("self", "cls"):
            position = 1 if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str) else 0
            location = node.body[position] if position < len(node.body) else node
            call = ast.Expr(ast.Call(ast.Name(RECEIVER, ast.Load()), [ast.Name(positional[0].arg, ast.Load())], []))
            node.body.insert(position, ast.copy_location(call, location))
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node):
        node = self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in self.attribute_names:
            return ast.copy_location(ast.Call(ast.Name(ATTRIBUTE, ast.Load()), [node.value, ast.Constant(node.attr)], []), node)
        return node

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Name) and (node.func.id in ("getattr", "hasattr") or node.func.id == "vars" and node.args):
            # Passing the callable preserves lexical builtin shadowing and
            # evaluates the original function before its arguments as usual.
            return ast.copy_location(ast.Call(ast.Name(CALL, ast.Load()), [node.func, *node.args], node.keywords), node)
        return node


class ClassUsage:
    def __init__(self, source: Path, plugin):
        self.source, self.plugin = source.resolve(), plugin
        self.module_paths = {}
        self.inventory = {}
        self.attribute_names = {"__dict__", "__mro__", "__bases__", "__class__", "__subclasses__"}
        for directory in (self.source / "sqlglot", self.source / "tests"):
            for path in directory.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                relative = path.relative_to(self.source).as_posix()
                module = relative[:-3].replace("/", ".")
                if module.endswith(".__init__"):
                    module = module[:-9]
                self.module_paths[module] = relative
                try:
                    tree = ast.parse(path.read_bytes(), filename=relative)
                    fields, classes = _class_fields(tree)
                except (OSError, SyntaxError, UnicodeError):
                    continue
                if relative.startswith("sqlglot/"):
                    self.inventory[relative] = sorted(_qualified_classes(tree))
                self.attribute_names.update(fields | classes)
        self.class_contexts = defaultdict(set)
        self.attribute_contexts = defaultdict(set)
        self._metadata = {}
        self._seen_classes = defaultdict(set)
        self._seen_attributes = defaultdict(set)
        self._getattr, self._hasattr, self._vars = builtins.getattr, builtins.hasattr, builtins.vars
        self.finder = _UsageFinder(self)
        self.loaded_modules = set()

    def install(self):
        self.uninstall()
        sys.meta_path.insert(0, self.finder)

    def uninstall(self):
        if self.finder in sys.meta_path:
            sys.meta_path.remove(self.finder)

    def _class_metadata(self, cls):
        key = id(cls)
        cached = self._metadata.get(key)
        if cached is not None:
            return cached[1]
        result = []
        try:
            for owner in type.__getattribute__(cls, "__mro__"):
                module = type.__getattribute__(owner, "__module__")
                path = self.module_paths.get(module)
                if path and path.startswith("sqlglot/"):
                    result.append((path, type.__getattribute__(owner, "__qualname__")))
        except (AttributeError, TypeError):
            pass
        # Retain the class to prevent id reuse for temporary test subclasses.
        self._metadata[key] = (cls, tuple(result))
        return tuple(result)

    def receiver(self, value):
        try:
            cls = value if isinstance(value, type) else type(value)
            context = self.plugin.context
            seen = self._seen_classes[context]
            key = id(cls)
            if key in seen:
                return
            seen.add(key)
            for identity in self._class_metadata(cls):
                self.class_contexts[identity].add(context)
        except Exception:
            pass

    def _attribute(self, value, name):
        if not isinstance(name, str):
            return
        try:
            cls = value if isinstance(value, type) else type(value)
            context = self.plugin.context
            key = (id(cls), name)
            seen = self._seen_attributes[context]
            if key in seen:
                return
            seen.add(key)
            metadata = self._class_metadata(cls)
            if not metadata:
                return
            self.receiver(value)
            # Actual derived class plus the first MRO definition preserve
            # inherited-attribute ownership without treating every same-name
            # field in unrelated classes as a dependency.
            identities = {metadata[0]}
            for owner in type.__getattribute__(cls, "__mro__"):
                if name in type.__getattribute__(owner, "__dict__"):
                    module = type.__getattribute__(owner, "__module__")
                    path = self.module_paths.get(module)
                    if path and path.startswith("sqlglot/"):
                        identities.add((path, type.__getattribute__(owner, "__qualname__")))
                    break
            for path, qualname in identities:
                self.attribute_contexts[(path, qualname, name)].add(context)
        except Exception:
            pass

    def attribute(self, value, name):
        self._attribute(value, name)
        result = self._getattr(value, name)
        if isinstance(result, type):
            self.receiver(result)
        return result

    def call(self, function, /, *args, **kwargs):
        if function is self._getattr or function is self._hasattr:
            if len(args) >= 2:
                self._attribute(args[0], args[1])
        elif function is self._vars and args:
            self.receiver(args[0])
        result = function(*args, **kwargs)
        if isinstance(result, type):
            self.receiver(result)
        return result

    def export(self, indices):
        class_usage = {path: {name: [] for name in names} for path, names in self.inventory.items() if names}
        attribute_usage = {}
        class_global, attribute_global = {}, {}
        for (path, name), contexts in self.class_contexts.items():
            users = {index for context in contexts for index in self.plugin.consumers(context, indices)}
            if users:
                class_usage.setdefault(path, {})[name] = sorted(users)
            if any(context in ("", "@global") for context in contexts):
                class_global.setdefault(path, []).append(name)
        for (path, name, attribute), contexts in self.attribute_contexts.items():
            users = {index for context in contexts for index in self.plugin.consumers(context, indices)}
            if users:
                attribute_usage.setdefault(path, {}).setdefault(name, {})[attribute] = sorted(users)
            if any(context in ("", "@global") for context in contexts):
                attribute_global.setdefault(path, {}).setdefault(name, []).append(attribute)
        for names in class_global.values():
            names.sort()
        for names in attribute_global.values():
            for attributes in names.values():
                attributes.sort()
        return {"class_usage": class_usage, "attribute_usage": attribute_usage, "class_global_usage": class_global, "attribute_global_usage": attribute_global}


class _UsageLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, filename, observer):
        super().__init__(fullname, filename)
        self.observer = observer

    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        tree = _UsageTransformer(self.observer.attribute_names).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__[RECEIVER] = self.observer.receiver
        module.__dict__[ATTRIBUTE] = self.observer.attribute
        module.__dict__[CALL] = self.observer.call
        self.observer.loaded_modules.add(module.__name__)
        super().exec_module(module)


class _UsageFinder(importlib.abc.MetaPathFinder):
    def __init__(self, observer):
        self.observer = observer

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.observer.module_paths:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if not spec or not spec.origin or not spec.origin.endswith(".py"):
            return None
        try:
            if Path(spec.origin).resolve().relative_to(self.observer.source).as_posix() != self.observer.module_paths[fullname]:
                return None
        except ValueError:
            return None
        spec.loader = _UsageLoader(fullname, spec.origin, self.observer)
        return spec
