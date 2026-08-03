# backend/tests/test_prompts.py
from agent.market_intelligence.prompts import PROFILE_PROMPT, SELECTION_PROMPT

def test_profile_prompt_renders():
    text = PROFILE_PROMPT.format(products="[{...}]")
    assert "价格分布" in text

def test_selection_prompt_has_output_shape():
    text = SELECTION_PROMPT.format(category="蓝牙耳机", profile="...", internal="...")
    assert "机会评分" in text
    assert "建议价格带" in text
