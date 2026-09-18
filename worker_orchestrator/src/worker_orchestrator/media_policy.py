from __future__ import annotations

from .models import Goal

AUTO_MEDIA_POLICY_ID = "auto-media-ui-only-v1"


def requires_visual_media(goal: Goal) -> bool:
    """Canonical automatic media trigger.

    SIMULATION_READY, READY, DONE, or any other lifecycle marker is deliberately
    irrelevant. Visual evidence is automatic only for an explicitly classified
    UI/visual goal or an explicit visual-media acceptance requirement.
    """
    return bool(goal.ui_visual_scope or goal.visual_media_required)


def media_policy_lines(goal: Goal) -> tuple[str, ...]:
    return (
        f"AUTO_MEDIA_POLICY_ID: {AUTO_MEDIA_POLICY_ID}",
        f"UI_VISUAL_SCOPE: {'true' if goal.ui_visual_scope else 'false'}",
        f"VISUAL_MEDIA_REQUIRED: {'true' if goal.visual_media_required else 'false'}",
        "VISUAL_MEDIA_RULE: Generate/archive visual media only when UI_VISUAL_SCOPE=true or VISUAL_MEDIA_REQUIRED=true; lifecycle markers alone never trigger media.",
    )
