"""Live evaluation against the real LLM (DeepSeek). Costs tokens; opt-in.

    RUN_DB_TESTS=1 RUN_LIVE_TESTS=1 python -m pytest tests/test_agent_live.py -v
or, against the compose database:
    docker compose run --rm api python -m app.cli eval-agent
"""

import os

import pytest

from app.agent.evaluation import SAMPLES, run_live_eval
from app.config import get_settings

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="live LLM tests are opt-in: RUN_LIVE_TESTS=1")


@pytest.mark.parametrize("sample", SAMPLES, ids=[s.id for s in SAMPLES])
async def test_real_model_on_sample(db, sample):
    if not get_settings().deepseek_api_key.get_secret_value():
        pytest.skip("DEEPSEEK_API_KEY not set")
    [result] = await run_live_eval(samples=[sample])
    assert result["passed"], f"{result['problems']} | {result['detail']}"
