#!/usr/bin/env python3
"""Measure qualified read sites during pinned imports and test collection.

This reads no patches or failure labels and executes no tests. Run in a fresh
interpreter with the pinned project's test dependencies. The existing ClassUsage
loader supplies baseline-only AST instrumentation; observations are recorded
before its per-context attribute deduplication.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def git(source: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *arguments], text=True
    ).strip()


def measure(source: Path, instrumentation: Path, expected_commit: str) -> dict:
    source, instrumentation = source.resolve(), instrumentation.resolve()
    commit = git(source, "rev-parse", "HEAD")
    if commit != expected_commit:
        raise ValueError(f"source commit is {commit}, expected {expected_commit}")
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("tracked source files are modified")
    if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
        raise ValueError("use a fresh interpreter without SQLGlot already imported")

    paths = [Path(name) for name in git(source, "ls-files", "--", "sqlglot", "tests").splitlines()
             if name.endswith(".py")]
    modules = sorted({path.as_posix()[:-3].replace("/", ".").removesuffix(".__init__")
                      for path in paths if path.parts[0] == "sqlglot"}, key=lambda name: (name.count("."), name))
    if not modules:
        raise ValueError("no tracked SQLGlot Python modules found")

    spec = importlib.util.spec_from_file_location("_baseline_class_usage", instrumentation)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load ClassUsage instrumentation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observer = module.ClassUsage(source, SimpleNamespace(context="@global"))
    original_attribute = observer._attribute
    sites: dict[tuple[str, str, str], set[tuple[str, int]]] = {}
    unknown_sites: list[dict] = []

    def record(value, attribute):
        # This wrapper runs BEFORE ClassUsage._attribute's seen-set check.
        if isinstance(attribute, str):
            cls = value if isinstance(value, type) else type(value)
            metadata = observer._class_metadata(cls)
            if metadata:
                identities = {metadata[0]}
                for owner in type.__getattribute__(cls, "__mro__"):
                    if attribute in type.__getattribute__(owner, "__dict__"):
                        owner_module = type.__getattribute__(owner, "__module__")
                        owner_path = observer.module_paths.get(owner_module)
                        if owner_path and owner_path.startswith("sqlglot/"):
                            identities.add((owner_path, type.__getattribute__(owner, "__qualname__")))
                        break
                frame = sys._getframe(1)
                while frame and Path(frame.f_code.co_filename).resolve() == instrumentation:
                    frame = frame.f_back
                caller = Path(frame.f_code.co_filename).resolve() if frame else None
                if caller and caller.is_relative_to(source) and caller.suffix == ".py":
                    location = (caller.relative_to(source).as_posix(), frame.f_lineno)
                    for path, owner in identities:
                        sites.setdefault((path, owner, attribute), set()).add(location)
                else:
                    unknown_sites.append({"owners": sorted(identities), "attribute": attribute})
        return original_attribute(value, attribute)

    observer._attribute = record
    failures, imported, cli_handling = [], [], None
    collection = {"exit_code": None, "tests_collected": 0, "test_modules": [],
                  "unobserved_modules": [], "errors": [], "execution_attempts": 0}
    sys.path.insert(0, str(source))
    observer.install()
    try:
        for name in modules:
            try:
                if name == "sqlglot.__main__":
                    # This tracked file is an executable CLI, not a passive
                    # library module. Import it with inert empty SQL. A plain
                    # source checkout lacks packaging's generated _version.py;
                    # provide only that display string in memory and restore
                    # the package immediately after the CLI import finishes.
                    package = importlib.import_module("sqlglot")
                    missing_version = not hasattr(package, "__version__")
                    if missing_version:
                        package.__version__ = f"0+baseline.{commit[:12]}"
                    arguments, captured = sys.argv, io.StringIO()
                    sys.argv = ["sqlglot", ""]
                    cli_handling = {"argv": ["sqlglot", ""],
                                    "temporary_display_version": missing_version}
                    try:
                        with contextlib.redirect_stdout(captured):
                            loaded = importlib.import_module(name)
                    finally:
                        sys.argv = arguments
                        if missing_version:
                            del package.__version__
                    cli_handling["stdout_characters"] = len(captured.getvalue())
                else:
                    loaded = importlib.import_module(name)
                filename = getattr(loaded, "__file__", None)
                if not filename or not Path(filename).resolve().is_relative_to(source):
                    raise ImportError("module resolved outside pinned checkout")
                imported.append(name)
            except Exception as error:
                failures.append({"module": name, "error": f"{type(error).__name__}: {error}"})

        # Match the baseline builder's environment. No test call is permitted;
        # collection imports module-level cached objects and fixture factories.
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ["PYTHONPATH"] = str(source) + os.pathsep + os.environ.get("PYTHONPATH", "")
        os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", os.defpath)
        sys.dont_write_bytecode = True
        import pytest

        class CollectionObserver:
            @pytest.hookimpl(trylast=True)
            def pytest_configure(self, config):
                # Pytest installs its assertion loader during configuration.
                # Reinsert the observer first so test attribute reads are seen.
                observer.install()

            def pytest_collectreport(self, report):
                if report.failed:
                    collection["errors"].append({"nodeid": report.nodeid, "error": str(report.longrepr)})

            def pytest_collection_finish(self, session):
                names = set()
                collection["tests_collected"] = len(session.items)
                for item in session.items:
                    loaded = getattr(item, "module", None)
                    if loaded is None:
                        collection["errors"].append({"nodeid": item.nodeid, "error": "collected item has no module"})
                    else:
                        names.add(loaded.__name__)
                collection["test_modules"] = sorted(names)

            @pytest.hookimpl(tryfirst=True)
            def pytest_runtest_protocol(self, item, nextitem):
                collection["execution_attempts"] += 1
                raise RuntimeError("this tool permits collection only, never test execution")

        output, errors = io.StringIO(), io.StringIO()
        previous_directory = Path.cwd()
        try:
            os.chdir(source)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                collection["exit_code"] = int(pytest.main(
                    ["--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "tests"],
                    plugins=[CollectionObserver()],
                ))
        finally:
            os.chdir(previous_directory)
        collection["stdout_characters"] = len(output.getvalue())
        collection["stderr_characters"] = len(errors.getvalue())
        collection["unobserved_modules"] = sorted(set(collection["test_modules"]) - observer.loaded_modules)
    finally:
        observer.uninstall()

    nested: dict = {}
    for (path, owner, attribute), locations in sorted(sites.items()):
        nested.setdefault(path, {}).setdefault(owner, {})[attribute] = [
            {"path": reader, "line": line} for reader, line in sorted(locations)
        ]
    unobserved = sorted(set(modules) - observer.loaded_modules)
    collection_complete = (collection["exit_code"] == 0 and collection["tests_collected"] > 0
                           and not collection["errors"] and not collection["unobserved_modules"]
                           and collection["execution_attempts"] == 0)
    return {
        "version": 1,
        "pinned_commit": commit,
        "complete": not failures and not unknown_sites and not unobserved and len(imported) == len(modules) and collection_complete,
        "collection_complete": collection_complete,
        "scope": "all tracked sqlglot Python modules imported, then all tests collected; no tests executed",
        "instrumentation": {
            "name": "ClassUsage AST loader; qualified reads before deduplication",
            "sha256": hashlib.sha256(instrumentation.read_bytes()).hexdigest(),
            "python": sys.version.split()[0],
        },
        "source_sha256": {path.as_posix(): hashlib.sha256((source / path).read_bytes()).hexdigest()
                          for path in sorted(paths)},
        "modules": modules,
        "imported_modules": imported,
        "failures": failures,
        "unknown_sites": unknown_sites,
        "unobserved_modules": unobserved,
        "cli_handling": cli_handling,
        "collection": collection,
        "sites": nested,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--class-usage-tool", required=True, type=Path)
    parser.add_argument("--commit", default=PINNED_COMMIT)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    data = measure(options.source, options.class_usage_tool, options.commit)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    if options.output.suffix == ".gz":
        payload = gzip.compress(payload, mtime=0)
    options.output.write_bytes(payload)
    count = sum(len(locations) for owners in data["sites"].values()
                for attributes in owners.values() for locations in attributes.values())
    print(f"Imported {len(data['imported_modules'])}/{len(data['modules'])} modules; "
          f"collected {data['collection']['tests_collected']} tests without execution; "
          f"recorded {count} qualified read sites; complete={data['complete']}")
    if not data["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
