import os

import pytest

from redcell.llm import LLMResponse


@pytest.fixture(autouse=True)
def _hermetic_agent_env(monkeypatch):
    """Clear AGENT_* vars so a developer's .env cannot change test outcomes.

    Importing litellm runs load_dotenv(), which copies the working directory's
    .env into os.environ, so without this the suite passes or fails depending on
    whose machine it runs on.
    """
    for key in [k for k in os.environ if k.startswith("AGENT_")]:
        monkeypatch.delenv(key, raising=False)


class StubLLM:
    """LLM double that returns a scripted list of LLMResponse objects in order."""

    def __init__(self, scripted: list[LLMResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        return self._scripted.pop(0)
