"""Fake CLAUDE_HOME fixture + HTTP client for the one test seam: the server's API."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

import context_saver

# A fake CLI "binary": bundled skills are registered via nu({...}) calls, with
# descriptions sometimes inlined and sometimes indirected through a minified var.
FAKE_BINARY = """
some minified junk;var qX9="Bundled charting guidance. Use when the user wants a chart.";
function a(){nu({name:"fake-dataviz",menuDescription:"Chart guidance",description:qX9,userInvocable:!0})}
function b(){nu({name:"fake-loop",menuDescription:"Run on an interval",description:"Repeat a prompt on a schedule.",userInvocable:!0})}
more junk
"""


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def write_skill(path: Path, name: str, description: str, **extra: object) -> None:
    """Write a SKILL.md with YAML frontmatter."""
    lines = [f"name: {name}", f"description: {description}"]
    for key, value in extra.items():
        key = key.replace("_", "-")
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n# " + name + "\n\nBody text.\n")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A synthetic CLAUDE_HOME: settings, usage data, skills, plugins, MCP, shadowing projects."""
    home = tmp_path / "home"
    claude = home / ".claude"

    # --- user-scope settings (carries unmanaged keys that must never be touched) ---
    write_json(
        claude / "settings.json",
        {
            "env": {"KEEP_ME": "1"},
            "permissions": {"allow": ["Bash(git:*)"]},
            "skillOverrides": {"stale-skill": "off"},
            "enabledPlugins": {
                "toolkit@market": True,
                "offkit@market": False,
            },
        },
    )

    # --- usage tracking (relative to the real clock, which the server reads) ---
    now_ms = int(time.time() * 1000)
    write_json(
        home / ".claude.json",
        {
            "skillUsage": {
                "daily-driver": {"usageCount": 27, "lastUsedAt": now_ms - 86_400_000},
                "stale-skill": {"usageCount": 2, "lastUsedAt": now_ms - 90 * 86_400_000},
            },
            "mcpServers": {
                "db": {"command": "db-server"},
                "cluster": {"command": "cluster-server"},
            },
            "projects": {
                str(home): {},
                str(tmp_path / "proj-shadow"): {},
                str(tmp_path / "proj-clean"): {},
            },
        },
    )

    # --- user skills ---
    write_skill(claude / "skills" / "daily-driver" / "SKILL.md", "daily-driver", "A skill used constantly.")
    write_skill(
        claude / "skills" / "stale-skill" / "SKILL.md",
        "stale-skill",
        "A skill nobody has touched in ages.",
    )
    write_skill(claude / "skills" / "never-used" / "SKILL.md", "never-used", "Never invoked, pure ballast.")
    write_skill(
        claude / "skills" / "locked-skill" / "SKILL.md",
        "locked-skill",
        "Author says humans only.",
        disable_model_invocation=True,
    )

    # --- plugins: one enabled, one disabled; skills live under installPath ---
    plugins = claude / "plugins"
    on_path = plugins / "cache" / "market" / "toolkit" / "1.0.0"
    off_path = plugins / "cache" / "market" / "offkit" / "1.0.0"
    # installed but absent from enabledPlugins entirely
    stray_path = plugins / "cache" / "market" / "straykit" / "1.0.0"
    write_json(
        plugins / "installed_plugins.json",
        {
            "version": 1,
            "plugins": {
                "toolkit@market": [{"scope": "user", "installPath": str(on_path), "version": "1.0.0"}],
                "offkit@market": [{"scope": "user", "installPath": str(off_path), "version": "1.0.0"}],
                "straykit@market": [{"scope": "user", "installPath": str(stray_path), "version": "1.0.0"}],
            },
        },
    )
    write_skill(stray_path / "skills" / "drifter" / "SKILL.md", "drifter", "Installed but never enabled.")
    write_skill(on_path / "skills" / "hammer" / "SKILL.md", "hammer", "Hits things, plugin-provided.")
    write_skill(
        on_path / "skills" / "chisel" / "SKILL.md",
        "chisel",
        "Carves things.",
        when_to_use="Use when carving is required.",
    )
    write_skill(off_path / "skills" / "ghost" / "SKILL.md", "ghost", "Should not be listed at all.")
    # plugin-provided MCP server
    write_json(on_path / ".mcp.json", {"mcpServers": {"toolkit-api": {"command": "toolkit-mcp"}}})

    # --- shadowing project settings ---
    write_json(
        tmp_path / "proj-shadow" / ".claude" / "settings.local.json",
        {"skillOverrides": {"daily-driver": "off"}},
    )
    write_json(
        tmp_path / "proj-shadow" / ".claude" / "settings.json",
        {"enabledPlugins": {"toolkit@market": False}},
    )
    write_json(tmp_path / "proj-clean" / ".claude" / "settings.json", {"permissions": {"allow": []}})

    # --- fake CLI binary for bundled-skill extraction ---
    binary = home / ".local" / "share" / "claude" / "versions" / "9.9.9"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(FAKE_BINARY)
    bin_link = home / ".local" / "bin" / "claude"
    bin_link.parent.mkdir(parents=True, exist_ok=True)
    bin_link.symlink_to(binary)

    return home


@dataclass
class Client:
    """Minimal HTTP client for the server under test."""

    base: str

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path) as r:  # noqa: S310 - localhost test server
            return json.loads(r.read())

    def post(self, path: str, body: dict, headers: dict[str, str] | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:  # noqa: S310 - localhost test server
            return json.loads(r.read())

    def post_expect_error(
        self, path: str, body: dict, headers: dict[str, str] | None = None
    ) -> tuple[int, dict]:
        try:
            self.post(path, body, headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
        raise AssertionError("expected an HTTP error")

    def get_raw(self, path: str) -> tuple[int, str, str]:
        with urllib.request.urlopen(self.base + path) as r:  # noqa: S310 - localhost test server
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()


@pytest.fixture
def client(home: Path):
    server = context_saver.make_server(context_saver.Paths(home=home), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(base=f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()


def items_by_name(state: dict, kind: str) -> dict[str, dict]:
    return {i["name"]: i for i in state["items"] if i["kind"] == kind}
