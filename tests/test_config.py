from redcell.config import Settings


def test_defaults_and_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("AGENT_TEMPERATURE", "0.2")
    s = Settings()
    assert s.model == "openai/gpt-4o"
    assert s.temperature == 0.2
    assert s.max_iterations == 10  # default
    assert s.log_level == "INFO"  # default


def test_gateway_defaults():
    s = Settings(_env_file=None)
    assert s.gateway_bin == "agentgateway"
    assert s.gateway_config_path == "agentgateway/config.yaml"
    assert s.gateway_host == "127.0.0.1"
    assert s.gateway_port == 3030
    assert s.gateway_url == "http://127.0.0.1:3030/mcp"
    assert s.gateway_autostart is True
    assert s.gateway_ready_timeout == 30.0


def test_gateway_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_PORT", "4040")
    monkeypatch.setenv("AGENT_GATEWAY_AUTOSTART", "false")
    s = Settings(_env_file=None)
    assert s.gateway_port == 4040
    assert s.gateway_autostart is False
