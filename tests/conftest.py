from redcell.llm import LLMResponse


class StubLLM:
    """LLM double that returns a scripted list of LLMResponse objects in order."""

    def __init__(self, scripted: list[LLMResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        return self._scripted.pop(0)
