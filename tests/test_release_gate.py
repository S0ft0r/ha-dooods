"""Tests for the validated-main release gate."""

from __future__ import annotations

import unittest

from scripts.release_gate import should_release


class ReleaseGateTests(unittest.TestCase):
    def test_allows_a_newer_version_even_when_an_intermediate_patch_is_skipped(self) -> None:
        allowed, _ = should_release("0.1.4", ["v0.1.2"])
        self.assertTrue(allowed)

    def test_allows_only_a_newer_version(self) -> None:
        allowed, _ = should_release("0.1.2", ["v0.1.0", "v0.1.1"])
        self.assertTrue(allowed)

    def test_skips_duplicate_or_older_release_versions(self) -> None:
        allowed, _ = should_release("0.1.1", ["v0.1.0", "v0.1.1"])
        self.assertFalse(allowed)
        allowed, _ = should_release("0.1.0", ["v0.1.2"])
        self.assertFalse(allowed)

    def test_ignores_unrelated_tag_formats(self) -> None:
        allowed, _ = should_release("0.1.1", ["v1.0.0", "beta"])
        self.assertTrue(allowed)

    def test_rejects_versions_outside_required_format(self) -> None:
        with self.assertRaises(ValueError):
            should_release("0.2.0", [])


if __name__ == "__main__":
    unittest.main()
