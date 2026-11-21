import pathlib
import unittest

from patch_analysis import PatchError, apply_patch_to_text, parse_patch


class ParsePatchTests(unittest.TestCase):
    def test_multiple_files_with_context_and_multiple_hunks(self):
        patch = """diff --git a/one.py b/one.py
--- a/one.py
+++ b/one.py
@@ -2,3 +2,4 @@ def first():
 before
-old
+new
+extra
 after
@@ -10 +11 @@ def last():
-last
+end
diff --git a/two.sql b/two.sql
--- a/two.sql
+++ b/two.sql
@@ -1 +1 @@
-SELECT a
+SELECT b
"""
        first, second = parse_patch(patch)
        self.assertEqual(first["old_path"], "one.py")
        self.assertEqual(first["old_lines"], [3, 10])
        self.assertEqual(first["new_lines"], [3, 4, 11])
        self.assertEqual(first["insertion_anchors"], [3, 10])
        self.assertEqual(first["hunks"][0]["section"], "def first():")
        self.assertEqual(second["new_path"], "two.sql")
        self.assertFalse(any(entry["malformed"] for entry in (first, second)))

    def test_insert_before_first_line_uses_zero_boundary(self):
        entry = parse_patch("--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+x\n+y\n")[0]
        self.assertEqual(entry["old_lines"], [])
        self.assertEqual(entry["new_lines"], [1, 2])
        self.assertEqual(entry["insertion_anchors"], [0])
        self.assertEqual(entry["old_anchors"], [1])
        self.assertEqual(apply_patch_to_text("old\n", entry), "x\ny\nold\n")

    def test_insert_between_lines_without_context(self):
        entry = parse_patch("--- a/a\n+++ b/a\n@@ -2,0 +3,2 @@\n+x\n+y\n")[0]
        self.assertEqual(entry["insertion_anchors"], [2])
        self.assertEqual(entry["old_anchors"], [2, 3])
        self.assertEqual(apply_patch_to_text("a\nb\nc\n", entry), "a\nb\nx\ny\nc\n")

    def test_insert_with_context_anchors_before_next_old_line(self):
        entry = parse_patch("--- a/a\n+++ b/a\n@@ -2,2 +2,3 @@\n b\n+x\n c\n")[0]
        self.assertEqual(entry["insertion_anchors"], [2])
        self.assertEqual(apply_patch_to_text("a\nb\nc\n", entry), "a\nb\nx\nc\n")

    def test_delete_at_start_and_end(self):
        entry = parse_patch("--- a/a\n+++ b/a\n@@ -1 +0,0 @@\n-a\n@@ -3 +1,0 @@\n-c\n")[0]
        self.assertEqual(entry["old_lines"], [1, 3])
        self.assertEqual(entry["new_lines"], [])
        self.assertEqual(apply_patch_to_text("a\nb\nc\n", entry), "b\n")

    def test_new_file_and_delete_file(self):
        patch = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+hello
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-goodbye
"""
        new, deleted = parse_patch(patch)
        self.assertIsNone(new["old_path"])
        self.assertTrue(new["is_new"])
        self.assertEqual(apply_patch_to_text("", new), "hello\n")
        self.assertIsNone(deleted["new_path"])
        self.assertTrue(deleted["is_deleted"])
        self.assertEqual(apply_patch_to_text("goodbye\n", deleted), "")

    def test_rename_only_and_copy(self):
        rename, copied = parse_patch(
            "diff --git a/old name.py b/new name.py\nsimilarity index 100%\n"
            "rename from old name.py\nrename to new name.py\n"
            "diff --git a/old.py b/copy.py\nsimilarity index 100%\n"
            "copy from old.py\ncopy to copy.py\n"
        )
        self.assertEqual((rename["old_path"], rename["new_path"]), ("old name.py", "new name.py"))
        self.assertTrue(rename["is_rename"])
        self.assertFalse(rename["malformed"])
        self.assertEqual(apply_patch_to_text("keep\n", rename), "keep\n")
        self.assertTrue(copied["is_copy"])
        self.assertFalse(copied["is_rename"])

    def test_quoted_git_paths_decode_octal_and_escaped_characters(self):
        entry = parse_patch(
            'diff --git "a/caf\\303\\251\\tname.py" "b/caf\\303\\251\\tname.py"\n'
            '--- "a/caf\\303\\251\\tname.py"\n+++ "b/caf\\303\\251\\tname.py"\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )[0]
        self.assertEqual(entry["old_path"], "café\tname.py")
        self.assertFalse(entry["malformed"])

    def test_plain_unified_diff_with_timestamps_and_space_paths(self):
        patch = (
            "--- one file\t2026-01-01\n+++ one file\t2026-01-02\n@@ -1 +1 @@\n-a\n+b\n"
            "--- two\t2026-01-01\n+++ two\t2026-01-02\n@@ -1 +1 @@\n-c\n+d\n"
        )
        entries = parse_patch(patch)
        self.assertEqual([entry["old_path"] for entry in entries], ["one file", "two"])
        self.assertTrue(all(not entry["malformed"] for entry in entries))

    def test_no_newline_markers(self):
        entry = parse_patch(
            "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n"
            "+new\n\\ No newline at end of file\n"
        )[0]
        self.assertEqual(apply_patch_to_text("old", entry), "new")
        self.assertTrue(entry["hunks"][0]["lines"][0]["no_newline"])

    def test_comment_and_fixture_changes_are_retained(self):
        patch = (
            "--- a/tests/fixtures/case.sql\n+++ b/tests/fixtures/case.sql\n"
            "@@ -1 +1 @@\n--- old expected error\n+-- new expected error\n"
        )
        entry = parse_patch(patch)[0]
        self.assertEqual(entry["old_lines"], [1])
        self.assertEqual(entry["removed_lines"][0]["text"], "-- old expected error")
        self.assertEqual(entry["added_lines"][0]["text"], "-- new expected error")
        self.assertEqual(apply_patch_to_text("-- old expected error\n", entry), "-- new expected error\n")

    def test_malformed_and_unsupported_input_is_not_silently_empty(self):
        malformed = [
            "this is not a patch",
            "--- a/a\n+++ b/a\n@@ invalid @@\n-x\n+y\n",
            "--- a/a\n+++ b/a\n@@ -1,2 +1,2 @@\n-x\n+y\n",
            "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n+extra\n",
            "diff --cc a.py\n@@@ -1,1 -1,1 +1,1 @@@\n-x\n+y\n",
            "--- a/a\n",
            "--- a/a\n+++ b/a\n+unanchored addition\n",
        ]
        for patch in malformed:
            with self.subTest(patch=patch):
                entry = parse_patch(patch)[0]
                self.assertTrue(entry["malformed"])
                self.assertTrue(entry["errors"])
                with self.assertRaises(PatchError):
                    apply_patch_to_text("x\n", entry)

    def test_binary_and_mode_changes(self):
        binary, mode = parse_patch(
            "diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n"
            "diff --git a/tool.py b/tool.py\nold mode 100644\nnew mode 100755\n"
        )
        self.assertTrue(binary["is_binary"])
        with self.assertRaises(PatchError):
            apply_patch_to_text("", binary)
        self.assertFalse(mode["malformed"])
        self.assertTrue(mode["mode_changed"])
        self.assertEqual((mode["old_mode"], mode["new_mode"]), ("100644", "100755"))
        self.assertEqual(apply_patch_to_text("pass\n", mode), "pass\n")

    def test_patch_application_rejects_wrong_context_and_overlap(self):
        entry = parse_patch("--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n")[0]
        with self.assertRaises(PatchError):
            apply_patch_to_text("wrong\n", entry)
        overlap = parse_patch(
            "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n@@ -1 +1 @@\n-a\n+c\n"
        )[0]
        with self.assertRaises(PatchError):
            apply_patch_to_text("a\n", overlap)

    def test_patch_application_validates_new_line_positions(self):
        entry = parse_patch("--- a/a\n+++ b/a\n@@ -1 +20 @@\n-a\n+b\n")[0]
        with self.assertRaises(PatchError):
            apply_patch_to_text("a\n", entry)

    def test_source_control_characters_do_not_split_lines(self):
        entry = parse_patch("--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\vold\n+b\vnew\n")[0]
        self.assertEqual(apply_patch_to_text("a\vold\n", entry), "b\vnew\n")

    def test_every_official_sample_patch_parses(self):
        sample_dir = pathlib.Path(__file__).resolve().parents[1] / "sample" / "commits"
        patches = list(sample_dir.glob("*.patch"))
        self.assertTrue(patches)
        for path in patches:
            with self.subTest(path=path.name):
                entries = parse_patch(path.read_text())
                self.assertTrue(entries)
                self.assertTrue(all(not entry["malformed"] for entry in entries))


if __name__ == "__main__":
    unittest.main()
