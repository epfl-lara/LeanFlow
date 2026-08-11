"""Tests for the V4A patch format parser."""

from types import SimpleNamespace

from tools.utilities.patch_parser import (
    OperationType,
    apply_v4a_operations,
    parse_v4a_patch,
    preview_v4a_update,
)


class TestParseUpdateFile:
    def test_basic_update(self):
        patch = """\
*** Begin Patch
*** Update File: src/main.py
@@ def greet @@
 def greet():
-    print("hello")
+    print("hi")
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        assert len(ops) == 1

        op = ops[0]
        assert op.operation == OperationType.UPDATE
        assert op.file_path == "src/main.py"
        assert len(op.hunks) == 1

        hunk = op.hunks[0]
        assert hunk.context_hint == "def greet"
        prefixes = [l.prefix for l in hunk.lines]
        assert " " in prefixes
        assert "-" in prefixes
        assert "+" in prefixes

    def test_multiple_hunks(self):
        patch = """\
*** Begin Patch
*** Update File: f.py
@@ first @@
 a
-b
+c
@@ second @@
 x
-y
+z
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        assert len(ops) == 1
        assert len(ops[0].hunks) == 2
        assert ops[0].hunks[0].context_hint == "first"
        assert ops[0].hunks[1].context_hint == "second"


class TestParseAddFile:
    def test_add_file(self):
        patch = """\
*** Begin Patch
*** Add File: new/module.py
+import os
+
+print("hello")
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        assert len(ops) == 1

        op = ops[0]
        assert op.operation == OperationType.ADD
        assert op.file_path == "new/module.py"
        assert len(op.hunks) == 1

        contents = [l.content for l in op.hunks[0].lines if l.prefix == "+"]
        assert contents[0] == "import os"
        assert contents[2] == 'print("hello")'


class TestParseDeleteFile:
    def test_delete_file(self):
        patch = """\
*** Begin Patch
*** Delete File: old/stuff.py
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        assert len(ops) == 1
        assert ops[0].operation == OperationType.DELETE
        assert ops[0].file_path == "old/stuff.py"


class TestParseMoveFile:
    def test_move_file(self):
        patch = """\
*** Begin Patch
*** Move File: old/path.py -> new/path.py
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        assert len(ops) == 1
        assert ops[0].operation == OperationType.MOVE
        assert ops[0].file_path == "old/path.py"
        assert ops[0].new_path == "new/path.py"


class TestParseInvalidPatch:
    def test_empty_patch_returns_empty_ops(self):
        ops, err = parse_v4a_patch("")
        assert err is None
        assert ops == []

    def test_no_begin_marker_still_parses(self):
        patch = """\
*** Update File: f.py
 line1
-old
+new
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        assert len(ops) == 1

    def test_multiple_operations(self):
        patch = """\
*** Begin Patch
*** Add File: a.py
+content_a
*** Delete File: b.py
*** Update File: c.py
 keep
-remove
+add
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        assert len(ops) == 3
        assert ops[0].operation == OperationType.ADD
        assert ops[1].operation == OperationType.DELETE
        assert ops[2].operation == OperationType.UPDATE


class TestApplyUpdate:
    def test_preserves_non_prefix_pipe_characters_in_unmodified_lines(self):
        patch = """\
*** Begin Patch
*** Update File: sample.py
@@ result @@
     result = 1
-    return result
+    return result + 1
*** End Patch"""
        operations, err = parse_v4a_patch(patch)
        assert err is None

        class FakeFileOps:
            def __init__(self):
                self.written = None

            def read_file(self, path, offset=1, limit=500):
                return SimpleNamespace(
                    content=(
                        "def run():\n"
                        '    cmd = "echo a | sed s/a/b/"\n'
                        "    result = 1\n"
                        "    return result"
                    ),
                    error=None,
                )

            def write_file(self, path, content):
                self.written = content
                return SimpleNamespace(error=None)

        file_ops = FakeFileOps()

        result = apply_v4a_operations(operations, file_ops)

        assert result.success is True
        assert file_ops.written == (
            'def run():\n    cmd = "echo a | sed s/a/b/"\n    result = 1\n    return result + 1'
        )


class _FakeFileOps:
    """Minimal file_ops stub: no _exec, so _apply_update uses the read_file branch."""

    def __init__(self, content: str):
        self._content = content
        self.written: str | None = None

    def read_file(self, path, offset=1, limit=500):
        return SimpleNamespace(content=self._content, error=None)

    def write_file(self, path, content):
        self.written = content
        return SimpleNamespace(error=None)


class TestAnchorScopedApply:
    """D3: a hunk is located inside its anchor region so duplicate text is disambiguated."""

    def test_anchor_picks_the_right_of_two_identical_bodies(self):
        # Both alpha() and beta() have the identical body `x = compute()\n    return x`.
        # Only the @@ anchor distinguishes them; the hunk must land in beta().
        content = (
            "def alpha():\n"
            "    x = compute()\n"
            "    return x\n"
            "\n"
            "def beta():\n"
            "    x = compute()\n"
            "    return x\n"
        )
        patch = """\
*** Begin Patch
*** Update File: s.py
@@ def beta @@
     x = compute()
-    return x
+    return x + 1
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        fo = _FakeFileOps(content)

        result = apply_v4a_operations(ops, fo)

        assert result.success is True
        # Exactly one occurrence changed, and it is the one inside beta().
        assert fo.written.count("return x + 1") == 1
        assert "def alpha():\n    x = compute()\n    return x\n" in fo.written
        assert "def beta():\n    x = compute()\n    return x + 1\n" in fo.written

    def test_exact_anchor_outranks_earlier_comment_substring(self):
        """Anchor a live declaration even when a preserved attempt names it first."""
        content = (
            "-- Failed attempt:\n"
            "-- theorem result : True := by\n"
            "--   sorry\n"
            "\n"
            "theorem result : True := by\n"
            "  sorry\n"
        )
        patch = """\
*** Begin Patch
*** Update File: Main.lean
@@
+private lemma checked : True := by
+  trivial
+
 theorem result : True := by
*** End Patch"""

        preview, error = preview_v4a_update(patch, content)

        assert error is None
        assert preview == (
            "-- Failed attempt:\n"
            "-- theorem result : True := by\n"
            "--   sorry\n"
            "\n"
            "private lemma checked : True := by\n"
            "  trivial\n"
            "\n"
            "theorem result : True := by\n"
            "  sorry\n"
        )

    def test_anchored_hunk_does_not_edit_unrelated_exact_match_elsewhere(self):
        # target()'s body differs from the hunk by trailing whitespace (so only a fuzzy strategy
        # matches it), while unrelated() contains the EXACT old text. The @@ anchor must keep the
        # edit inside target(); a whole-file exact match must NOT hijack an anchored hunk.
        content = (
            "def target():\n"
            "    y = compute()  \n"  # trailing spaces -> only line_trimmed fuzzy matches here
            "    return y \n"
            "\n"
            "def unrelated():\n"
            "    y = compute()\n"  # EXACT match for the hunk search text
            "    return y\n"
        )
        patch = """\
*** Begin Patch
*** Update File: s.py
@@ def target @@
     y = compute()
-    return y
+    return y + 1
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        fo = _FakeFileOps(content)

        result = apply_v4a_operations(ops, fo)

        assert result.success is True
        # The edit landed in target(); the unrelated exact match is untouched.
        assert "def unrelated():\n    y = compute()\n    return y\n" in fo.written
        assert fo.written.count("return y + 1") == 1
        assert "return y + 1" in fo.written.split("def unrelated():")[0]

    def test_ambiguous_duplicate_without_anchor_is_refused(self):
        # Same duplicate body, but the anchor does not disambiguate (points nowhere),
        # so the exact-duplicate case must fail rather than silently edit one at random.
        content = "def a():\n    return z\n\ndef b():\n    return z\n"
        patch = """\
*** Begin Patch
*** Update File: s.py
@@ nonexistent_anchor @@
-    return z
+    return z2
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        fo = _FakeFileOps(content)

        result = apply_v4a_operations(ops, fo)

        assert result.success is False
        assert fo.written is None
        assert result.error is not None


class TestStrictV4AApply:
    """D3: strict=True makes UPDATE hunks exact-or-fail."""

    def test_strict_rejects_whitespace_only_hunk(self):
        content = "def f():\n    return  1\n"  # double space on disk
        patch = """\
*** Begin Patch
*** Update File: s.py
@@ def f @@
-    return 1
+    return 2
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None

        # Non-strict tolerates the whitespace difference (structural match) ...
        fo_ok = _FakeFileOps(content)
        assert apply_v4a_operations(ops, fo_ok, strict=False).success is True

        # ... strict refuses it.
        fo_strict = _FakeFileOps(content)
        result = apply_v4a_operations(ops, fo_strict, strict=True)
        assert result.success is False
        assert fo_strict.written is None

    def test_strict_still_applies_exact_hunk(self):
        content = "def f():\n    return 1\n"
        patch = """\
*** Begin Patch
*** Update File: s.py
@@ def f @@
-    return 1
+    return 2
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        fo = _FakeFileOps(content)

        result = apply_v4a_operations(ops, fo, strict=True)
        assert result.success is True
        assert "    return 2\n" in fo.written


class TestNearMissOnFailure:
    """D3: a hunk that can't be found surfaces the closest region."""

    def test_failure_reports_near_miss_snippet(self):
        content = "def compute_total(items):\n    return sum(items)\n"
        patch = """\
*** Begin Patch
*** Update File: s.py
@@ def compute_total @@
-    return total(items)
+    return sum(items) + 1
*** End Patch"""
        ops, err = parse_v4a_patch(patch)
        assert err is None
        fo = _FakeFileOps(content)

        result = apply_v4a_operations(ops, fo, strict=True)

        assert result.success is False
        assert result.error is not None
        # A concrete "did you mean" snippet, not a generic message.
        assert "Closest region" in result.error
        assert "return sum(items)" in result.error


def test_implicit_trailing_declaration_anchor_allows_unique_exact_preceding_insertion():
    """Insert before a declaration when the implicit anchor is trailing context."""
    content = """\
private lemma prior : True := by
  trivial

theorem result : True := by
  trivial
"""
    blank_context = " "
    patch = f"""\
*** Begin Patch
*** Update File: Main.lean
@@
   trivial
+
+private lemma inserted : True := by
+  trivial
{blank_context}
 theorem result : True := by
*** End Patch"""
    ops, err = parse_v4a_patch(patch)
    assert err is None
    fo = _FakeFileOps(content)

    result = apply_v4a_operations(ops, fo, strict=True)

    assert result.success is True
    assert fo.written.count("private lemma inserted") == 1
    assert fo.written.index("private lemma inserted") < fo.written.index("theorem result")


def test_explicit_anchor_does_not_escape_for_preceding_context():
    """Keep explicit anchor regions authoritative for ambiguous preceding edits."""
    content = """\
private lemma prior : True := by
  trivial

theorem result : True := by
  trivial
"""
    blank_context = " "
    patch = f"""\
*** Begin Patch
*** Update File: Main.lean
@@ theorem result : True := by @@
   trivial
{blank_context}
 theorem result : True := by
*** End Patch"""
    ops, err = parse_v4a_patch(patch)
    assert err is None
    fo = _FakeFileOps(content)

    result = apply_v4a_operations(ops, fo, strict=True)

    assert result.success is False
    assert "anchor region" in (result.error or "")
