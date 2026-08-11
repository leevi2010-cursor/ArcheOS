from __future__ import annotations

from .models import ChangeProposal


class BusinessLanguageHumanJudgmentPort:
    def render(self, proposal: ChangeProposal) -> str:
        review = proposal.human_review
        return "\n".join(
            (
                f"系统发现：{review.finding}",
                f"为什么重要：{review.importance}",
                f"建议：{review.recommendation}",
                f"依据：{review.evidence}",
                f"选择及后果：{review.consequences}",
                "可选择：批准、拒绝或稍后决定。",
            )
        )

    def normalize_decision(self, decision: str) -> str:
        value = decision.strip().lower()
        if value not in {"approve", "reject", "defer"}:
            raise ValueError("decision must be approve, reject, or defer")
        return value
