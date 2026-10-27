"""Versioned system prompts. Bump PROMPT_VERSION whenever the prompt text changes;
agent_runs.prompt_version records which one produced each run."""

from functools import lru_cache
from pathlib import Path

PROMPT_VERSION = "v1"


@lru_cache
def system_prompt(version: str = PROMPT_VERSION) -> str:
    return (Path(__file__).with_name(f"system_{version}.md")).read_text(encoding="utf-8")
