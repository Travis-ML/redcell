"""Runnable example: an agent with two simple tools.

    uv run python examples/demo_agent.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from redcell import LLM, Agent, Settings, ToolRegistry, logging_hooks, tool


@tool
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@tool
def utc_now() -> str:
    """Return the current UTC time in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


async def main() -> None:
    settings = Settings()
    agent = Agent(
        llm=LLM(
            settings.model,
            settings.temperature,
            settings.max_tokens,
            api_base=settings.api_base,
            api_key=settings.api_key,
        ),
        tools=ToolRegistry([add, utc_now]),
        system_prompt="You are a concise assistant. Use tools when helpful.",
        hooks=logging_hooks(),
        max_iterations=settings.max_iterations,
    )
    print(await agent.run("What is 21 + 21, and what time is it in UTC?"))


if __name__ == "__main__":
    asyncio.run(main())
