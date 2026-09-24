"""Rendering the effective AgentGateway config from the bundled one.

AgentGateway aggregates every stdio target behind one endpoint, and a target that
exits during `initialize` fails the aggregated call for all of them. The renderer
keeps that from happening by dropping targets that cannot start, and fills in the
Qdrant address, which the static file cannot know (localhost in host mode, the
compose service name in Docker).
"""

from pathlib import Path

import yaml

from redcell.gatewayconfig import render_gateway_config, write_effective_config

SSH_CFG = ".redcell/openshell_ssh_config"


def _config() -> dict:
    return {
        "binds": [
            {
                "port": 3030,
                "listeners": [
                    {
                        "routes": [
                            {
                                "backends": [
                                    {
                                        "mcp": {
                                            "targets": [
                                                {
                                                    "name": "playwright",
                                                    "stdio": {
                                                        "cmd": "npx",
                                                        "args": ["-y", "@playwright/mcp@0.0.78"],
                                                    },
                                                },
                                                {
                                                    "name": "filesystem",
                                                    "stdio": {
                                                        "cmd": "ssh",
                                                        "args": [
                                                            "-F",
                                                            SSH_CFG,
                                                            "-T",
                                                            "host",
                                                            "mcp-server-filesystem",
                                                        ],
                                                    },
                                                },
                                                {
                                                    "name": "rag",
                                                    "stdio": {
                                                        "cmd": "env",
                                                        "args": [
                                                            "QDRANT_URL=http://localhost:6333",
                                                            "COLLECTION_NAME=kb",
                                                            "uvx",
                                                            "mcp-server-qdrant",
                                                        ],
                                                    },
                                                },
                                                {
                                                    "name": "shell",
                                                    "stdio": {
                                                        "cmd": "ssh",
                                                        "args": [
                                                            "-F",
                                                            SSH_CFG,
                                                            "-T",
                                                            "host",
                                                            "mcp-server-commands",
                                                        ],
                                                    },
                                                },
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    }


def _targets(cfg: dict) -> dict[str, dict]:
    tgts = cfg["binds"][0]["listeners"][0]["routes"][0]["backends"][0]["mcp"]["targets"]
    return {t["name"]: t for t in tgts}


def _all_on_path(cmd: str) -> str | None:
    return f"/usr/bin/{cmd}"


def test_sandbox_targets_dropped_when_sandbox_unavailable():
    cfg, dropped = render_gateway_config(
        _config(),
        sandbox_available=False,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=_all_on_path,
    )
    assert set(_targets(cfg)) == {"playwright", "rag"}
    assert {name for name, _ in dropped} == {"filesystem", "shell"}


def test_sandbox_targets_kept_when_sandbox_available():
    cfg, dropped = render_gateway_config(
        _config(),
        sandbox_available=True,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=_all_on_path,
    )
    assert set(_targets(cfg)) == {"playwright", "filesystem", "rag", "shell"}
    assert dropped == []


def test_target_with_missing_runtime_dropped():
    def no_npx(cmd: str) -> str | None:
        return None if cmd == "npx" else f"/usr/bin/{cmd}"

    cfg, dropped = render_gateway_config(
        _config(),
        sandbox_available=True,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=no_npx,
    )
    assert "playwright" not in _targets(cfg)
    assert dropped[0][0] == "playwright" and "npx" in dropped[0][1]


def test_env_wrapped_runtime_is_checked_too():
    # `env ... uvx mcp-server-qdrant` really needs uvx, not just env.
    def no_uvx(cmd: str) -> str | None:
        return None if cmd == "uvx" else f"/usr/bin/{cmd}"

    cfg, dropped = render_gateway_config(
        _config(),
        sandbox_available=True,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=no_uvx,
    )
    assert "rag" not in _targets(cfg)
    assert [name for name, _ in dropped] == ["rag"]


def test_qdrant_url_rewritten():
    cfg, _ = render_gateway_config(
        _config(),
        sandbox_available=True,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=_all_on_path,
    )
    args = _targets(cfg)["rag"]["stdio"]["args"]
    assert "QDRANT_URL=http://qdrant:6333" in args
    assert "QDRANT_URL=http://localhost:6333" not in args


def test_source_config_not_mutated():
    src = _config()
    render_gateway_config(
        src,
        sandbox_available=False,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=_all_on_path,
    )
    assert len(_targets(src)) == 4


def test_write_effective_config(tmp_path: Path):
    src = tmp_path / "config.yaml"
    src.write_text(yaml.safe_dump(_config()))
    out = tmp_path / "state" / "agentgateway.yaml"
    path, dropped = write_effective_config(
        src,
        out,
        sandbox_available=False,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=_all_on_path,
    )
    assert path == str(out)
    assert set(_targets(yaml.safe_load(out.read_text()))) == {"playwright", "rag"}
    assert {n for n, _ in dropped} == {"filesystem", "shell"}


def test_write_effective_config_falls_back_to_source_on_bad_yaml(tmp_path: Path):
    # Rendering is a convenience; a config it cannot parse is passed through
    # untouched so AgentGateway reports the real error instead of redcell.
    src = tmp_path / "config.yaml"
    src.write_text("binds: [unclosed")
    path, dropped = write_effective_config(
        src,
        tmp_path / "out.yaml",
        sandbox_available=True,
        ssh_config_path=SSH_CFG,
        qdrant_url="http://qdrant:6333",
        which=_all_on_path,
    )
    assert path == str(src)
    assert dropped == []
