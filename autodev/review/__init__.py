"""人工代码审查闭环包（WP-2：Diff 解析、风险策略判定、行内评论与返工编排）。"""

from autodev.review.diffs import milestone_diff, parse_unified_diff, task_diff
from autodev.review.policy import effective_review_policy, evaluate_review_policy
from autodev.review.service import (
    ReviewConflictError,
    add_comment,
    decide_review,
    get_review,
    list_reviews,
    open_review,
    update_comment,
)

__all__ = [
    "ReviewConflictError",
    "add_comment",
    "decide_review",
    "effective_review_policy",
    "evaluate_review_policy",
    "get_review",
    "list_reviews",
    "milestone_diff",
    "open_review",
    "parse_unified_diff",
    "task_diff",
    "update_comment",
]
