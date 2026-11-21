"""Parse unified diffs without assuming that any kind of edit is harmless.

``old_lines`` and ``new_lines`` contain the lines actually removed/added.
``insertion_anchors`` are boundaries in the old file: 0 is before line 1,
N is after line N.  ``old_anchors`` includes the neighboring old line numbers
around those boundaries, for coverage consumers to clip to the file length.

Malformed or unsupported input is retained and marked, rather than silently
turning an unrecognized change into an empty change set.
"""

from __future__ import annotations

import ast
import re
from typing import Any


class PatchError(ValueError):
    """A patch cannot be safely interpreted or applied to the supplied text."""


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_QUOTED_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\S+')


def _path(value: str, prefix: str = "") -> str | None:
    """Read git's C-quoted paths and unified-diff tab-separated timestamps."""
    if value.startswith('"'):
        token = _QUOTED_TOKEN.match(value)
        if not token:
            raise PatchError("Unclosed quoted path")
        try:
            value = ast.literal_eval(token.group())
        except (ValueError, SyntaxError) as error:
            raise PatchError("Invalid quoted path") from error
        # Git encodes non-ASCII pathname bytes as octal escapes in quotes.
        try:
            value = value.encode("latin1").decode("utf8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    else:
        value = value.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if prefix and value.startswith(prefix):
        value = value[len(prefix):]
    if not value:
        raise PatchError("Empty file path")
    return value


def _git_paths(value: str) -> tuple[str | None, str | None]:
    tokens = _QUOTED_TOKEN.findall(value)
    if len(tokens) == 2:
        return _path(tokens[0], "a/"), _path(tokens[1], "b/")
    # Git may leave spaces unquoted. File headers supersede this fallback.
    if value.startswith("a/") and " b/" in value:
        old, new = value.rsplit(" b/", 1)
        return _path(old, "a/"), _path("b/" + new, "b/")
    raise PatchError("Cannot read diff --git paths")


def _entry(old: str | None = None, new: str | None = None) -> dict[str, Any]:
    return {
        "old_path": old,
        "new_path": new,
        "old_lines": [],
        "new_lines": [],
        "insertion_anchors": [],
        "old_anchors": [],
        "removed_lines": [],
        "added_lines": [],
        "hunks": [],
        "is_new": False,
        "is_deleted": False,
        "is_rename": False,
        "is_copy": False,
        "is_binary": False,
        "old_mode": None,
        "new_mode": None,
        "mode_changed": False,
        "malformed": False,
        "errors": [],
    }


def parse_patch(text: str) -> list[dict[str, Any]]:
    """Return a per-file description of a git or plain unified diff.

    Hunk ``lines`` preserve content as ``{kind, text, no_newline}`` dictionaries.
    A removed/context line is in the old file; an added/context line is in the
    new file. Header counts are validated. All coordinates are 1-based except
    the explicitly documented insertion boundaries.
    """
    if not isinstance(text, str):
        raise TypeError("Patch text must be str")
    if not text.strip():
        return []

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    old_pos = new_pos = old_seen = new_seen = 0
    saw_headers = False
    saw_new_header = False

    def error(message: str) -> None:
        assert current is not None
        current["errors"].append(message)
        current["malformed"] = True

    def finish_hunk() -> None:
        nonlocal hunk
        if hunk is None:
            return
        if old_seen != hunk["old_count"] or new_seen != hunk["new_count"]:
            error("Hunk line counts do not match its header")
        hunk = None

    def finish_file() -> None:
        nonlocal current, saw_headers, saw_new_header
        if current is None:
            return
        finish_hunk()
        if saw_headers and not saw_new_header:
            error("Old-file header without new-file header")
        if saw_headers and not current["hunks"] and not current["is_binary"]:
            error("File headers without any textual hunk")
        if current["old_path"] is None and current["new_path"] is None:
            error("Both file paths are missing")
        current["is_new"] = current["is_new"] or current["old_path"] is None
        current["is_deleted"] = current["is_deleted"] or current["new_path"] is None
        current["is_rename"] = current["is_rename"] or (
            current["old_path"] is not None
            and current["new_path"] is not None
            and current["old_path"] != current["new_path"]
            and not current["is_copy"]
        )
        current["mode_changed"] = current["old_mode"] != current["new_mode"]
        for key in ("old_lines", "new_lines", "insertion_anchors", "old_anchors"):
            current[key] = sorted(set(current[key]))
        entries.append(current)
        current = None
        saw_headers = False
        saw_new_header = False

    # Split only LF: source text may legitimately contain other control chars.
    patch_lines = text.split("\n")
    if patch_lines[-1] == "":
        patch_lines.pop()
    for line in patch_lines:
        if line.startswith("diff --git "):
            finish_file()
            current = _entry()
            try:
                current["old_path"], current["new_path"] = _git_paths(line[11:])
            except PatchError as exc:
                error(str(exc))
            continue
        if line.startswith(("diff --cc ", "diff --combined ")):
            finish_file()
            current = _entry()
            name = line.split(" ", 2)[2]
            try:
                current["old_path"] = current["new_path"] = _path(name)
            except PatchError as exc:
                error(str(exc))
            error("Combined diffs are unsupported")
            continue
        if line.startswith("@@"):
            if current is None:
                current = _entry()
            finish_hunk()
            match = _HUNK.match(line)
            if match is None:
                error("Malformed or unsupported hunk header")
                continue
            old_start, old_count, new_start, new_count, section = match.groups()
            hunk = {
                "old_start": int(old_start),
                "old_count": int(old_count) if old_count is not None else 1,
                "new_start": int(new_start),
                "new_count": int(new_count) if new_count is not None else 1,
                "section": section.strip(),
                "lines": [],
            }
            if (hunk["old_count"] and hunk["old_start"] < 1) or (
                hunk["new_count"] and hunk["new_start"] < 1
            ):
                error("Nonempty hunk uses a zero line number")
            current["hunks"].append(hunk)
            old_pos = hunk["old_start"] + (hunk["old_count"] == 0)
            new_pos = hunk["new_start"] + (hunk["new_count"] == 0)
            old_seen = new_seen = 0
            continue
        if hunk is not None:
            if line == "\\ No newline at end of file":
                if not hunk["lines"] or hunk["lines"][-1]["no_newline"]:
                    error("Misplaced no-newline marker")
                else:
                    hunk["lines"][-1]["no_newline"] = True
                continue
            complete = old_seen == hunk["old_count"] and new_seen == hunk["new_count"]
            # After a complete hunk, these are file headers, not removed text.
            if complete and line.startswith(("--- ", "+++ ")):
                finish_hunk()
            elif line and line[0] in " +-":
                kind, content = line[0], line[1:]
                hunk["lines"].append({"kind": kind, "text": content, "no_newline": False})
                if kind == "-":
                    current["old_lines"].append(old_pos)
                    current["removed_lines"].append({"line": old_pos, "text": content})
                elif kind == "+":
                    current["new_lines"].append(new_pos)
                    current["added_lines"].append({"line": new_pos, "text": content})
                    anchor = max(0, old_pos - 1)
                    current["insertion_anchors"].append(anchor)
                    current["old_anchors"].extend([anchor, anchor + 1] if anchor else [1])
                if kind in " -":
                    old_pos += 1
                    old_seen += 1
                if kind in " +":
                    new_pos += 1
                    new_seen += 1
                if old_seen > hunk["old_count"] or new_seen > hunk["new_count"]:
                    error("Hunk contains more lines than its header")
                continue
            else:
                finish_hunk()
                if not complete:
                    error("Unexpected line inside hunk")
        if line.startswith("--- "):
            # Plain unified diffs can concatenate files without diff --git.
            if current is not None and saw_headers:
                finish_file()
            if current is None:
                current = _entry()
            try:
                current["old_path"] = _path(line[4:], "a/")
            except PatchError as exc:
                error(str(exc))
            saw_headers = True
        elif line.startswith("+++ "):
            if current is None:
                current = _entry()
            if not saw_headers:
                error("New-file header without old-file header")
            saw_new_header = True
            try:
                current["new_path"] = _path(line[4:], "b/")
            except PatchError as exc:
                error(str(exc))
        elif current is not None:
            for marker, field in (("rename from ", "old_path"), ("rename to ", "new_path"),
                                  ("copy from ", "old_path"), ("copy to ", "new_path")):
                if line.startswith(marker):
                    try:
                        current[field] = _path(line[len(marker):])
                    except PatchError as exc:
                        error(str(exc))
                    current["is_copy" if marker.startswith("copy") else "is_rename"] = True
                    break
            if line.startswith("new file mode "):
                current["old_path"] = None
                current["is_new"] = True
                current["new_mode"] = line.removeprefix("new file mode ").strip()
            elif line.startswith("deleted file mode "):
                current["new_path"] = None
                current["is_deleted"] = True
                current["old_mode"] = line.removeprefix("deleted file mode ").strip()
            elif line.startswith("old mode "):
                current["old_mode"] = line.removeprefix("old mode ").strip()
            elif line.startswith("new mode "):
                current["new_mode"] = line.removeprefix("new mode ").strip()
            elif line.startswith(("Binary files ", "GIT binary patch")):
                current["is_binary"] = True
            elif line.startswith(("+", "-")) and not line.startswith("-- "):
                error("Changed line outside a hunk")
    finish_file()
    if not entries:
        current = _entry()
        error("No recognizable file diff found")
        finish_file()
    return entries


def apply_patch_to_text(original: str, entry: dict[str, Any]) -> str:
    """Apply one parsed textual file diff with exact context validation.

    This does not touch the filesystem. It rejects malformed/binary patches,
    overlapping hunks, and any mismatch with the pinned original source.
    """
    if entry.get("malformed") or entry.get("is_binary"):
        raise PatchError("Cannot apply malformed or binary diff")
    if entry.get("old_path") is None and original:
        raise PatchError("New file requires empty original text")
    parts = original.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    result: list[str] = []
    cursor = 0
    for hunk in entry["hunks"]:
        target = hunk["old_start"] - (hunk["old_count"] != 0)
        if target < cursor or target > len(lines):
            raise PatchError("Hunk is outside the original or overlaps an earlier hunk")
        result.extend(lines[cursor:target])
        cursor = target
        new_target = hunk["new_start"] - (hunk["new_count"] != 0)
        if new_target != len(result):
            raise PatchError("Hunk new-file position is inconsistent with earlier changes")
        before = cursor
        for change in hunk["lines"]:
            content = change["text"] + ("" if change["no_newline"] else "\n")
            if change["kind"] in " -":
                if cursor >= len(lines) or lines[cursor] != content:
                    raise PatchError("Hunk context does not match original text")
                cursor += 1
            if change["kind"] in " +":
                result.append(content)
        if cursor - before != hunk["old_count"]:
            raise PatchError("Hunk does not consume its declared old lines")
    result.extend(lines[cursor:])
    updated = "".join(result)
    if entry.get("is_deleted") and updated:
        raise PatchError("Deleted-file diff leaves content")
    return updated
