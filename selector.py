"""Select tests from pinned-source coverage and minimize worker cost.

No labeled sample answers are used by this program or its offline maps.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import io
import json
import sys
import tokenize
from pathlib import Path

from patch_analysis import PatchError, apply_patch_to_text, parse_patch
from scheduler import schedule
from class_dependencies import (
    changed_bindings, generated_classes, literal_binding_values, only_binding_values_changed,
)
from import_probe import imports_safely

ROOT = Path(__file__).resolve().parent
CLASS_IMPORT_CONTEXTS = ["", "@global", "@collect:*"]


class Selector:
    def __init__(self, tests: list[dict], source: Path, map_path: Path | None):
        self.tests, self.source = tests, source
        self.all_numbers = {test["number"] for test in tests}
        self.by_id = {test["id"]: test["number"] for test in tests}
        self.by_file: dict[str, set[int]] = {}
        for test in tests:
            self.by_file.setdefault(test["file"], set()).add(test["number"])
        self.files: dict = {}
        self.map_numbers: dict[int, int] = {}
        self.source_cache: dict[str, tuple[str, ast.Module]] = {}
        self.imports: dict[str, list[str]] = {}
        self.runtime_imports: dict[str, dict[str, list[int]]] = {}
        self.import_cache: dict[str, set[int]] = {}
        self.symbols: dict = {}
        self.class_usage: dict = {}
        self.attribute_global_usage: dict | None = None
        self.class_numbers: dict[int, int] = {}
        self._active_overrides: dict[str, str] = {}
        self._class_probe_cache: dict[tuple, bool] = {}
        if map_path and map_path.exists():
            opener = gzip.open if map_path.suffix == ".gz" else open
            with opener(map_path, "rt", encoding="utf-8") as handle:
                data = json.load(handle)
            self.files = data["files"]
            self.imports = data.get("imports", {})
            self.runtime_imports = data.get("runtime_imports", {})
            self.map_numbers = {
                index: self.by_id[nodeid]
                for index, nodeid in enumerate(data["tests"])
                if nodeid in self.by_id
            }
            self.class_usage = data.get("class_usage", {})
            if data.get("class_usage_import_contexts") == CLASS_IMPORT_CONTEXTS:
                self.attribute_global_usage = data.get("attribute_global_usage")
            self.class_numbers = self.map_numbers
            classes_path = map_path.parent / "classes.json.gz"
            if classes_path.exists():
                with gzip.open(classes_path, "rt", encoding="utf-8") as handle:
                    class_data = json.load(handle)
                if (class_data.get("commit") == data.get("commit")
                        and class_data.get("exit_code") == 0
                        and class_data.get("import_contexts") == CLASS_IMPORT_CONTEXTS):
                    self.class_usage = class_data["class_usage"]
                    self.attribute_global_usage = class_data.get("attribute_global_usage")
                    self.class_numbers = {
                        index: self.by_id[nodeid]
                        for index, nodeid in enumerate(class_data["tests"])
                        if nodeid in self.by_id
                    }
            symbols_path = map_path.parent / "symbols.json.gz"
            if symbols_path.exists():
                with gzip.open(symbols_path, "rt", encoding="utf-8") as handle:
                    self.symbols = json.load(handle)
        self.unmapped_numbers = self.all_numbers - set(self.map_numbers.values())

    def numbers(self, indices) -> set[int]:
        return {self.map_numbers[i] for i in indices if i in self.map_numbers}

    def readers(self, path: str) -> set[int]:
        return self.numbers(self.files.get(path, {}).get("reads", []))

    def file_tests(self, path: str) -> set[int]:
        result = self.readers(path) | self.by_file.get(path, set())
        for indices in self.files.get(path, {}).get("lines", {}).values():
            result.update(self.numbers(indices))
        return result

    def import_users(self, path: str) -> set[int]:
        if path not in self.import_cache:
            affected = {path}
            while True:
                more = {owner for owner, dependencies in self.imports.items()
                        if affected.intersection(dependencies)}
                # A local import executed while constructing a module is an
                # import-time edge; imports reached only during tests use their
                # measured caller contexts instead of every module importer.
                for owner, targets in self.runtime_imports.items():
                    global_lines = set(self.files.get(owner, {}).get("global", []))
                    if any(target in affected and global_lines.intersection(lines)
                           for target, lines in targets.items()):
                        more.add(owner)
                if more.issubset(affected):
                    break
                affected.update(more)
            result: set[int] = set()
            for owner in affected:
                result.update(self.by_file.get(owner, set()))
            for owner, targets in self.runtime_imports.items():
                line_map = self.files.get(owner, {}).get("lines", {})
                for target, lines in targets.items():
                    if target in affected:
                        for line in lines:
                            result.update(self.numbers(line_map.get(str(line), [])))
            self.import_cache[path] = result
        return self.import_cache[path]

    def fixture_users(self, path: str) -> set[int]:
        # Shared fixture setup is already expanded to every consumer by the
        # map builder. The pinned project's ordinary fixture helpers reopen on
        # each use, so unrelated tests in a reader's module need not be run.
        return self.readers(path)

    def attribute_users(self, names: set[str]) -> set[int]:
        if not self.symbols:
            return self.all_numbers
        locations: dict[str, set[int]] = {}
        for name in names:
            for path, lines in self.symbols.get("attributes", {}).get(name, {}).items():
                locations.setdefault(path, set()).update(lines)
        for path, lines in self.symbols.get("dynamic_attributes", {}).items():
            locations.setdefault(path, set()).update(lines)
        result: set[int] = set()
        for path, lines in locations.items():
            info = self.files.get(path, {})
            for line in lines:
                result.update(self.numbers(info.get("lines", {}).get(str(line), [])))
            if set(info.get("global", [])).intersection(lines):
                result.update(self.file_tests(path) | self.import_users(path))
        return result

    def class_users(self, path: str, owners) -> set[int] | None:
        """Return observed users of specific classes, or decline to narrow."""
        classes = self.class_usage.get(path, {})
        if not owners or any(owner not in classes for owner in owners):
            return None
        return {self.class_numbers[index] for owner in owners for index in classes[owner]
                if index in self.class_numbers}

    def _class_import_safe(self, path: str, new_text: str) -> bool:
        overrides = {**self._active_overrides, path: new_text}
        key = tuple(sorted(overrides.items()))
        if key not in self._class_probe_cache:
            self._class_probe_cache[key] = imports_safely(self.source, overrides)
        return self._class_probe_cache[key]

    def class_fields_uncached(self, path: str, bindings: dict) -> bool:
        """Decline narrowing when collection can copy data outside its class."""
        if self.attribute_global_usage is None:
            return False
        observed = self.attribute_global_usage.get(path, {})
        return all(not {attribute, "__dict__"}.intersection(observed.get(owner, []))
                   for owner, attribute in bindings)

    @staticmethod
    def changed_class_data(tree: ast.Module, lines: set[int]) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    continue
                if not any(statement.lineno <= line <= statement.end_lineno for line in lines):
                    continue
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                names.update(target.id for target in targets if isinstance(target, ast.Name))
        return names

    @staticmethod
    def impact_lines(change: dict) -> set[int]:
        """Use removed lines for replacements; anchors only for pure insertions."""
        result = set(change.get("old_lines", []))
        for hunk in change.get("hunks", []):
            cursor = hunk["old_start"]
            block: list[dict] = []
            block_start = cursor
            for item in [*hunk["lines"], {"kind": " ", "text": ""}]:
                if item["kind"] == " ":
                    if block and not any(part["kind"] == "-" for part in block):
                        result.update((max(1, block_start - 1), max(1, block_start)))
                    block = []
                    cursor += 1
                else:
                    if not block:
                        block_start = cursor
                    block.append(item)
                    if item["kind"] == "-":
                        cursor += 1
        return result

    def original(self, path: str) -> tuple[str, ast.Module]:
        if path not in self.source_cache:
            target = (self.source / path).resolve()
            if not target.is_relative_to(self.source.resolve()):
                raise ValueError("patch path escapes source")
            text = target.read_text(encoding="utf-8")
            self.source_cache[path] = text, ast.parse(text, type_comments=True)
        return self.source_cache[path]

    def affected(self, change: dict) -> tuple[set[int], str]:
        path = change.get("old_path") or change.get("new_path")
        if not path or change.get("malformed") or change.get("is_binary"):
            return self.all_numbers, "unrecognized patch"
        if not self.files:
            return self.all_numbers, "no coverage map"
        readers = self.readers(path)
        info = self.files.get(path, {})
        if not path.endswith(".py"):
            if info.get("global_read"):
                return self.all_numbers, "fixture read during collection"
            readers = self.fixture_users(path)
            return (readers, "fixture readers") if readers else (
                self.all_numbers, "unknown fixture"
            )
        if any(change.get(key) for key in (
            "is_new", "is_deleted", "is_rename", "is_copy", "mode_changed"
        )):
            return self.all_numbers, "module structure changed"
        try:
            old_text, old_tree = self.original(path)
            new_text = apply_patch_to_text(old_text, change)
            if tokenize.detect_encoding(io.BytesIO(old_text.encode()).readline)[0] != tokenize.detect_encoding(
                io.BytesIO(new_text.encode()).readline
            )[0]:
                return self.all_numbers, "source encoding changed"
            new_tree = ast.parse(new_text, type_comments=True)
            compile(new_tree, path, "exec", dont_inherit=True)
        except (OSError, ValueError, SyntaxError, LookupError, UnicodeError, PatchError):
            return self.all_numbers, "unparseable module"
        if ast.dump(old_tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False):
            # Compare parsed programs rather than treating a kind of edit as
            # harmless. Raw-source readers still need to run.
            if info.get("global_read"):
                return self.all_numbers, "source read during collection"
            return self.fixture_users(path), "equivalent AST plus source readers"
        if path.startswith("tests/"):
            return self.by_file.get(path, self.all_numbers) | readers, "changed test file"

        class_names = self.changed_class_data(old_tree, set(change.get("old_lines", [])))
        class_names.update(self.changed_class_data(new_tree, set(change.get("new_lines", []))))
        if class_names:
            bindings = changed_bindings(old_tree, new_tree, set(change.get("old_lines", [])),
                                        set(change.get("new_lines", [])))
            # Only replace the existing broad selection for existing bindings.
            # Adding/removing fields can affect users absent from baseline maps.
            # Generated methods can be absent from baseline receiver tracing.
            if (only_binding_values_changed(old_tree, new_tree, bindings)
                    and literal_binding_values(bindings)
                    and not generated_classes(old_tree)
                    and self.class_fields_uncached(path, bindings)):
                owners = {owner for old, new in bindings.values() for binding in [*old, *new]
                          for owner in binding.usage_owners}
                users = self.class_users(path, owners)
                if users is not None:
                    if not self._class_import_safe(path, new_text):
                        return self.all_numbers, "class import failed; broad fallback"
                    return users | readers, "qualified class users"
            result = self.file_tests(path) | self.import_users(path) | self.attribute_users(class_names)
            return result, "class data and attribute consumers"

        lines = self.impact_lines(change)
        functions = [node for node in ast.walk(old_tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
        expanded: set[int] = set()
        for line in lines:
            enclosing = [node for node in functions
                         if min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
                         <= line <= getattr(node, "end_lineno", node.lineno)]
            if not enclosing:
                result = self.file_tests(path) | self.import_users(path)
                return (result, "module users") if result else (self.all_numbers, "unknown module")
            node = min(enclosing, key=lambda item: item.end_lineno - item.lineno)
            body = getattr(node, "body", None)
            body_start = body[0].lineno if isinstance(body, list) and body else node.lineno
            if line < body_start:
                # Defaults, annotations, decorators and the def itself execute
                # when importing, even for tests that never call the function.
                result = self.file_tests(path) | self.import_users(path)
                return (result, "definition and import users") if result else (
                    self.all_numbers, "unknown definition users"
                )
            expanded.update(range(node.lineno, node.end_lineno + 1))
        selected = set(readers)
        for line in expanded:
            selected.update(self.numbers(info.get("lines", {}).get(str(line), [])))
        if selected:
            return selected, "affected function coverage"
        result = self.file_tests(path)
        return (result, "module fallback") if result else (self.all_numbers, "unmapped code")

    def select(self, text: str) -> tuple[list[dict], list[str]]:
        try:
            changes = parse_patch(text)
        except (ValueError, PatchError):
            return self.tests, ["invalid patch fallback"]
        if not changes:
            return self.tests, ["empty patch fallback"]
        self._active_overrides = {}
        for change in changes:
            path = change.get("old_path")
            if path and path.endswith(".py") and path == change.get("new_path"):
                try:
                    text, _ = self.original(path)
                    self._active_overrides[path] = apply_patch_to_text(text, change)
                except (OSError, ValueError, SyntaxError, LookupError, UnicodeError, PatchError):
                    pass
        numbers: set[int] = set(self.unmapped_numbers)
        reasons = []
        for change in changes:
            selected, reason = self.affected(change)
            numbers.update(selected)
            reasons.append(reason)
        return [test for test in self.tests if test["number"] in numbers], reasons


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--map", type=Path, default=ROOT / "maps/coverage.json.gz")
    parser.add_argument("--all-tests", action="store_true")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    selector = Selector(tests, args.source, None if args.all_tests else args.map)
    schedules: dict[tuple[int, ...], list[list[int]]] = {}
    for patch in sorted((args.data / "commits").glob("*.patch")):
        selected, reasons = selector.select(patch.read_text(encoding="utf-8"))
        key = tuple(test["number"] for test in selected)
        if key not in schedules:
            schedules[key] = schedule(selected)
        print(json.dumps({"commit": patch.stem, "workers": schedules[key]}, separators=(",", ":")))
        print(f"{patch.stem}: {len(selected)} tests; {'; '.join(reasons)}", file=sys.stderr)


if __name__ == "__main__":
    main()
