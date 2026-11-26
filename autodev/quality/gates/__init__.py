"""质量门禁插件集合（WP-4）。"""

from autodev.quality.gates import diff_coverage, lint, sast, secrets, test_integrity

__all__ = ["diff_coverage", "lint", "sast", "secrets", "test_integrity"]
