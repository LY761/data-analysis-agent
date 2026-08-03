# backend/tests/test_prompts.py
from agent.market_intelligence.prompts import (
    PROFILE_PROMPT, SELECTION_PROMPT, REVIEW_PAIN_PROMPT,
)

def test_profile_prompt_renders():
    text = PROFILE_PROMPT.format(products="[{...}]")
    assert "价格分布" in text

def test_selection_prompt_has_output_shape():
    text = SELECTION_PROMPT.format(category="蓝牙耳机", profile="...", internal="...")
    assert "机会评分" in text
    assert "建议价格带" in text

def test_review_pain_prompt_no_fabrication_instruction():
    """无真实评论时提示 LLM 诚实降级，不编造用户原话"""
    text = REVIEW_PAIN_PROMPT.format(reviews="[{...}]")
    assert "不要编造用户原话" in text
