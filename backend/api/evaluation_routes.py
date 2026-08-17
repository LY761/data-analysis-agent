"""Agent 内部评测与黄金演示 API。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from eval.golden_scenarios import GOLDEN_SCENARIOS
from eval.questions import EVAL_QUESTIONS
from eval.runner import run_agent_evaluation


router = APIRouter(tags=["evaluations"])


@router.post("/evaluations/agent/run")
async def run_agent_eval(
    limit: int = Query(100, ge=1, le=100),
):
    return run_agent_evaluation(cases=EVAL_QUESTIONS[:limit])


@router.get("/evaluations/golden-scenarios")
async def golden_scenarios():
    return {"scenarios": list(GOLDEN_SCENARIOS)}
