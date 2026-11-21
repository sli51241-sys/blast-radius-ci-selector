"""Check a modified class module imports before narrowing its test selection.

The child imports the supplied source overrides in memory in a fresh Python
interpreter. It runs no tests, reads no labels, and writes no source files.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


def imports_safely(source: Path, overrides: dict[str, str], timeout: float = 5.0) -> bool:
    command = [sys.executable, "-B", "-S", str(Path(__file__).resolve()),
               "--child", "--source", str(source.resolve())]
    try:
        result = subprocess.run(command, input=json.dumps(overrides).encode(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def child(source: Path, overrides: dict[str, str]) -> None:
    source = source.resolve()
    modules = {}
    for relative, text in overrides.items():
        file = (source / relative).resolve()
        if not file.is_relative_to(source) or file.suffix != ".py":
            raise ValueError("invalid source override")
        module = relative[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-len(".__init__")]
        modules[module] = (file, text)

    class Loader(importlib.abc.Loader):
        def __init__(self, file, text):
            self.file, self.text = file, text

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            exec(compile(self.text, str(self.file), "exec", dont_inherit=True), module.__dict__)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in modules:
                return None
            file, text = modules[fullname]
            locations = [str(file.parent)] if file.name == "__init__.py" else None
            return importlib.util.spec_from_file_location(fullname, file,
                loader=Loader(file, text), submodule_search_locations=locations)

    sys.path.insert(0, str(source))
    sys.meta_path.insert(0, Finder())
    for module in sorted(modules):
        importlib.import_module(module)
    if (source / "sqlglot").is_dir():
        # Derived dialect classes can process another class's configuration at
        # import time, even if their modules were not themselves modified.
        from sqlglot.dialects.dialect import Dialect
        _ = Dialect.classes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", required=True)
    parser.add_argument("--source", type=Path, required=True)
    options = parser.parse_args()
    child(options.source, json.load(sys.stdin))
