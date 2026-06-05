def test_public_api_importable():
    import redcell as a

    for name in (
        "Agent",
        "tool",
        "Tool",
        "ToolRegistry",
        "Memory",
        "InMemoryStore",
        "Settings",
        "LLM",
        "GatewaySupervisor",
        "MCPManager",
    ):
        assert hasattr(a, name), f"missing export: {name}"
