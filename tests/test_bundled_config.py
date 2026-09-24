"""Consistency checks across the bundled Dockerfile and gateway config."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_playwright_mcp_version_matches_docker_image():
    # The image bakes in a browser build that matches one @playwright/mcp
    # release. If the gateway config asks npx for a different release, the
    # browser tool fails at first use with "Chromium distribution not found".
    dockerfile = (ROOT / "Dockerfile").read_text()
    gateway = (ROOT / "agentgateway" / "config.yaml").read_text()
    image = re.findall(r"@playwright/mcp@([\w.\-]+)", dockerfile)
    config = re.findall(r"@playwright/mcp@([\w.\-]+)", gateway)
    assert image and config
    assert set(config) == set(image), f"gateway config {config} != Dockerfile {image}"


def test_env_example_keys_are_real_settings():
    # Settings ignores unknown keys, so a misspelled or renamed variable in
    # .env.example is silently a no-op for everyone who copies it.
    from redcell.config import Settings

    fields = {f"AGENT_{name.upper()}" for name in Settings.model_fields}
    keys = re.findall(r"^#?\s*(AGENT_[A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M)
    unknown = sorted(set(keys) - fields)
    assert keys and not unknown, f".env.example keys with no setting: {unknown}"
