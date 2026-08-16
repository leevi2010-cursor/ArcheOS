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
                "可选择："
                + "、".join(
                    {
                        "approve": "批准",
                        "reject": "拒绝",
                        "defer": "稍后决定",
                        "bind_existing": "绑定已有对象",
                        "create_minimal": "创建最小对象",
                        "edit_identity_and_create": "更正身份后创建",
                    }.get(action, action)
                    for action in review.allowed_actions
                )
                + "。",
            )
        )

    def normalize_decision(self, decision: str) -> str:
        value = decision.strip().lower()
        if value not in {
            "approve",
            "reject",
            "defer",
            "bind_existing",
            "create_minimal",
            "edit_identity_and_create",
        }:
            raise ValueError("decision is not supported")
        return value
