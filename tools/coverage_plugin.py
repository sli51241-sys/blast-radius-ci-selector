"""Offline pytest instrumentation for build_coverage.py.

This observes only the unmodified baseline checkout.  It contains no knowledge of
the sample patches or their answers.  Setup, call and teardown get separate
coverage contexts; shared pytest fixtures are attributed to every consumer.
"""

from __future__ import annotations

import os
import pickle
import sys
from concurrent.futures import Executor, Future
from collections import defaultdict
from pathlib import Path

import pytest


GLOBAL_CONTEXT = "@global"


class InlineProcessPoolExecutor(Executor):
    """Map-generation instrumentation: keep spawned work in the test context.

    Baseline tests submit independent CPU tasks through ProcessPoolExecutor.
    Ordinary parent-process coverage silently loses every worker's execution.
    Running those tasks synchronously keeps coverage and audited file reads
    attributed to the requesting test.  Pickle round trips preserve the real
    process boundary's serialization operations (including Expr.dump/load),
    which themselves can be important dependencies.

    This replacement is never used by the submitted runtime selector.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        self._max_workers = max_workers or os.cpu_count() or 1
        self._initializer = initializer
        self._initargs = initargs
        self._initialized = False
        self._shutdown = False

    @staticmethod
    def _roundtrip(value):
        return pickle.loads(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))

    def submit(self, fn, /, *args, **kwargs):
        if self._shutdown:
            raise RuntimeError("cannot schedule new futures after shutdown")
        future = Future()
        future.set_running_or_notify_cancel()
        try:
            if not self._initialized:
                self._initialized = True
                if self._initializer is not None:
                    initializer, initargs = self._roundtrip((self._initializer, self._initargs))
                    initializer(*initargs)
            func, call_args, call_kwargs = self._roundtrip((fn, args, kwargs))
            future.set_result(self._roundtrip(func(*call_args, **call_kwargs)))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._shutdown = True


def inline_process_pools():
    """Install before baseline test modules bind their executor imports."""
    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = InlineProcessPoolExecutor
    concurrent.futures.process.ProcessPoolExecutor = InlineProcessPoolExecutor


class CoveragePlugin:
    def __init__(self, coverage, source: Path):
        self.coverage = coverage
        self.source = str(source.resolve())
        self.prefix = self.source + os.sep
        self.context = GLOBAL_CONTEXT
        self.tests: list[str] = []
        self.outcomes: dict[str, dict[str, str]] = defaultdict(dict)
        self.read_contexts: dict[str, set[str]] = defaultdict(set)
        self.read_owners: dict[str, set[str]] = defaultdict(set)
        self.fixture_consumers: dict[str, set[str]] = defaultdict(set)
        self.fixture_contexts: dict[int, str] = {}
        self._path_cache: dict[str, str | None] = {}
        self._auditing = False
        sys.addaudithook(self._audit)

    def relative(self, value) -> str | None:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        try:
            value = os.fsdecode(value)
            if value in self._path_cache:
                return self._path_cache[value]
            absolute = os.path.abspath(value)
            relative = absolute[len(self.prefix) :] if absolute.startswith(self.prefix) else None
            if relative and any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.split(os.sep)):
                relative = None
            if relative:
                relative = relative.replace(os.sep, "/")
            # Absolute names dominate; do not cache relative paths across chdir.
            if os.path.isabs(value):
                self._path_cache[value] = relative
            return relative
        except (OSError, TypeError, ValueError):
            return None

    def switch(self, context: str):
        self.context = context
        self.coverage.switch_context(context)

    def _audit(self, event, arguments):
        if event != "open" or self._auditing:
            return
        self._auditing = True
        try:
            path = self.relative(arguments[0])
            if path is None or path.endswith((".pyc", ".pyo")):
                return
            mode = arguments[1] if len(arguments) > 1 else None
            flags = arguments[2] if len(arguments) > 2 else 0
            if isinstance(mode, str):
                readable = "r" in mode or "+" in mode
            else:
                readable = isinstance(flags, int) and (flags & os.O_ACCMODE) != os.O_WRONLY
            if not readable:
                return
            self.read_contexts[path].add(self.context)
            # Owners retain a way to expand reads made once and cached by a
            # helper.  The selector can follow callers/imports conservatively.
            frame = sys._getframe(1)
            while frame is not None:
                owner = self.relative(frame.f_code.co_filename)
                if owner and owner.endswith(".py"):
                    self.read_owners[path].add(owner)
                frame = frame.f_back
        except Exception:
            # Observation must never change the baseline's test behavior.
            pass
        finally:
            self._auditing = False

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_make_collect_report(self, collector):
        previous = self.context
        relative = self.relative(getattr(collector, "path", ""))
        if relative and relative.endswith(".py"):
            self.switch("@collect:" + relative)
        try:
            yield
        finally:
            self.switch(previous)

    def pytest_collection_finish(self, session):
        self.tests = [item.nodeid for item in session.items]
        for item in session.items:
            info = getattr(item, "_fixtureinfo", None)
            if info is None:
                continue
            for name in info.names_closure:
                definitions = info.name2fixturedefs.get(name) or ()
                for definition in definitions:
                    if definition.scope == "function":
                        continue
                    key = id(definition)
                    context = self.fixture_contexts.setdefault(key, "@fixture:" + str(key))
                    self.fixture_consumers[context].add(item.nodeid)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        previous = self.context
        context = self.fixture_contexts.get(id(fixturedef))
        if context:
            self.switch(context)
        try:
            yield
        finally:
            self.switch(previous)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.switch(item.nodeid + "|setup")
        try:
            yield
        finally:
            self.switch(GLOBAL_CONTEXT)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.switch(item.nodeid + "|setup")

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        self.switch(item.nodeid + "|call")

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        self.switch(item.nodeid + "|teardown")

    def pytest_runtest_logreport(self, report):
        # Pytest 9 can emit failed subtest reports followed by a passed parent
        # call report.  Preserve the failure for baseline-health auditing.
        if self.outcomes[report.nodeid].get(report.when) != "failed" or report.failed:
            self.outcomes[report.nodeid][report.when] = report.outcome

    def consumers(self, context: str, indices: dict[str, int]) -> list[int]:
        if context.startswith("@fixture:"):
            return sorted(indices[node] for node in self.fixture_consumers[context] if node in indices)
        if context.startswith("@collect:"):
            path = context.removeprefix("@collect:")
            return [index for node, index in indices.items() if node.split("::", 1)[0] == path]
        node = context.rsplit("|", 1)[0]
        return [indices[node]] if node in indices else []
