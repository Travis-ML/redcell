"""redcell: async-first, vendor-agnostic agent scaffold."""

from .agent import Agent, ChatResult
from .config import Settings
from .gateway import GatewaySupervisor
from .llm import LLM, LLMResponse, ToolCall
from .mcp import MCPManager
from .memory import InMemoryStore, Memory
from .observability import Hooks, configure_logging, logging_hooks
from .searxng import make_web_search
from .server import create_app
from .tools import Tool, ToolRegistry, tool

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "ChatResult",
    "create_app",
    "Settings",
    "LLM",
    "LLMResponse",
    "ToolCall",
    "InMemoryStore",
    "Memory",
    "Hooks",
    "configure_logging",
    "logging_hooks",
    "Tool",
    "ToolRegistry",
    "tool",
    "make_web_search",
    "GatewaySupervisor",
    "MCPManager",
]
