#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Context Saver - a local click-click UI for managing Claude Code context load.

Scans skills (user, plugin, bundled), plugins and MCP servers, estimates what each
costs in always-paid context tokens, and writes your choices to user-scope
~/.claude/settings.json with a backup, a diff preview and one-click undo.

    uv run context_saver.py            # opens http://127.0.0.1:8787

Only three settings keys are ever written: skillOverrides, enabledPlugins,
disabledMcpjsonServers (plus disableBundledSkills for the master toggle).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import time
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

# --------------------------------------------------------------------------- #
# constants mirroring the CLI's own behaviour
# --------------------------------------------------------------------------- #

SKILL_STATES = ("on", "name-only", "user-invocable-only", "off")
LOCKED_STATES = ("user-invocable-only", "off")
# The CLI truncates each listing description at skillListingMaxDescChars (default 1536).
DESC_CAP = 1536
MANAGED_KEYS = ("skillOverrides", "enabledPlugins", "disabledMcpjsonServers", "disableBundledSkills")
STALE_DAYS = 60
BACKUP_PREFIX = "context-saver-"

# The CLI caps the whole skill listing at skillListingBudgetFraction (0.01) of the
# context window, measured at 4 bytes/token - i.e. 1% of the window in tokens. Over
# budget it keeps bundled and name-only entries whole and truncates the rest to
# `- name`, so trimming while over the cap buys back descriptions rather than tokens.
BUDGET_FRACTION = 0.01
DEFAULT_CONTEXT_WINDOW = 1_000_000


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Paths:
    """Every filesystem root the tool touches, hanging off one overridable home."""

    home: Path

    @property
    def claude_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def settings_file(self) -> Path:
        return self.claude_dir / "settings.json"

    @property
    def claude_json(self) -> Path:
        return self.home / ".claude.json"

    @property
    def skills_dir(self) -> Path:
        return self.claude_dir / "skills"

    @property
    def installed_plugins_file(self) -> Path:
        return self.claude_dir / "plugins" / "installed_plugins.json"

    @property
    def backups_dir(self) -> Path:
        return self.claude_dir / "backups"

    @property
    def cli_binary(self) -> Path | None:
        """The CLI build currently pointed at by the `claude` launcher, if resolvable."""
        launcher = self.home / ".local" / "bin" / "claude"
        try:
            target = launcher.resolve()
        except OSError:
            return None
        return target if target.is_file() else None


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def est_tokens(text: str) -> int:
    """Token estimate: chars / 4, the same order of accuracy /context reports."""
    return (len(text) + 3) // 4


def read_json(path: Path) -> tuple[dict, str | None]:
    """Return (data, error). A missing file is an empty dict with no error."""
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return {}, f"{path}: {exc}"
    return (data, None) if isinstance(data, dict) else ({}, f"{path}: expected a JSON object")


def listing_line(name: str, description: str, when_to_use: str | None) -> str:
    """The exact line the CLI injects into context for an `on` skill."""
    body = f"{description} - {when_to_use}" if when_to_use else description
    return f"- {name}: {body[:DESC_CAP]}"


def state_tokens(name: str, description: str, when_to_use: str | None) -> dict[str, int]:
    return {
        "on": est_tokens(listing_line(name, description, when_to_use)),
        "name-only": est_tokens(f"- {name}"),
        "user-invocable-only": 0,
        "off": 0,
    }


def parse_frontmatter(path: Path) -> dict | None:
    """Parse a SKILL.md's YAML frontmatter block."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _clean(value: object) -> str:
    """Collapse a frontmatter scalar (possibly a YAML block) into one line."""
    return " ".join(str(value).split()) if value is not None else ""


# --------------------------------------------------------------------------- #
# bundled-skill extraction from the CLI build
# --------------------------------------------------------------------------- #

_IDENT = r"[A-Za-z_$][\w$]*"


def _read_js_string(text: str, i: int) -> tuple[str | None, int]:
    """Read a JS string literal starting at text[i]; returns (value, index_after)."""
    quote = text[i]
    if quote not in "\"'":
        return None, i
    out: list[str] = []
    i += 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2 : i + 6] or ""):
                out.append(chr(int(text[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
            continue
        if ch == quote:
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None, i


def _object_fields(text: str, start: int) -> dict[str, tuple[str, str]]:
    """Top-level fields of the object literal whose `{` is at text[start].

    Values come back tagged: ("str", value) for literals, ("ident", name) for
    variable references, ("other", "") for anything else (functions, templates).
    """
    fields: dict[str, tuple[str, str]] = {}
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            _, i = _read_js_string(text, i)
            continue
        if ch == "`":  # template literal: skip to its end, tolerating ${}
            i += 1
            while i < len(text) and text[i] != "`":
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if ch in "{[(":
            depth += 1
            i += 1
            continue
        if ch in "}])":
            depth -= 1
            if depth == 0:
                return fields
            i += 1
            continue
        if depth == 1:
            m = re.match(rf"({_IDENT})\s*:\s*", text[i:])
            prev = text[max(0, i - 40) : i].rstrip()[-1:]  # bounded: the build is ~270MB
            if m and (i == start + 1 or prev in ",{"):
                key = m.group(1)
                j = i + m.end()
                if j < len(text) and text[j] in "\"'":
                    value, j = _read_js_string(text, j)
                    fields[key] = ("str", value or "")
                else:
                    # an identifier reference, tolerating whitespace before the delimiter
                    ident = re.match(rf"({_IDENT})\s*(?=[,}}])", text[j:])
                    fields[key] = ("ident", ident.group(1)) if ident else ("other", "")
                i = j
                continue
        i += 1
    return fields


def _resolve_idents(text: str, idents: set[str]) -> dict[str, str]:
    """Resolve many `ident = "..."` assignments in ONE pass over the text.

    The CLI build is ~270MB; a scan per identifier costs seconds each, so every
    wanted name goes into a single alternation and we walk the text once.
    """
    if not idents:
        return {}
    alternation = "|".join(sorted(map(re.escape, idents), key=len, reverse=True))
    table: dict[str, str] = {}
    for m in re.finditer(rf"(?<![\w$])({alternation})\s*=\s*(?=[\"'])", text):
        if m.group(1) in table:
            continue
        value, _ = _read_js_string(text, m.end())
        if value is not None:
            table[m.group(1)] = value
    return table


def _extract_bundled(text: str) -> list[tuple[str, str]]:
    registrations = [_object_fields(text, m.end() - 1) for m in re.finditer(r"nu\(\{", text)]
    wanted = {
        field[1]
        for fields in registrations
        for key in ("name", "description")
        if (field := fields.get(key)) and field[0] == "ident"
    }
    table = _resolve_idents(text, wanted)

    def value(fields: dict[str, tuple[str, str]], key: str) -> str | None:
        field = fields.get(key)
        if field is None:
            return None
        kind, raw = field
        return raw if kind == "str" else (table.get(raw) if kind == "ident" else None)

    found: dict[str, str] = {}
    for fields in registrations:
        name, description = value(fields, "name"), value(fields, "description")
        if not name or not description or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            continue
        found.setdefault(name, " ".join(description.split()))
    return sorted(found.items())


@lru_cache(maxsize=4)
def _extract_bundled_memo(path: str, fingerprint: tuple[float, int]) -> tuple[tuple[str, str], ...]:
    try:
        text = Path(path).read_bytes().decode("latin-1")
    except OSError:
        return ()
    return tuple(_extract_bundled(text))


def extract_bundled_skills(paths: Paths) -> list[tuple[str, str]]:
    """(name, description) for every bundled skill readable out of the CLI build.

    Cached on disk against the build's (mtime, size): a fresh scan costs ~13s, so
    without this every page load would stall. A CLI upgrade invalidates it.
    """
    binary = paths.cli_binary
    if binary is None:
        return []
    try:
        stat = binary.stat()
    except OSError:
        return []

    fingerprint = [str(binary), stat.st_mtime, stat.st_size]
    cache_file = paths.claude_dir / "context-saver" / "bundled.json"
    cached, _ = read_json(cache_file)
    if cached.get("fingerprint") == fingerprint:
        return [(n, d) for n, d in cached.get("skills") or []]

    skills = list(_extract_bundled_memo(str(binary), (stat.st_mtime, stat.st_size)))
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"fingerprint": fingerprint, "skills": skills}))
    except OSError:
        pass  # a read-only home just means we rescan next time
    return skills


# --------------------------------------------------------------------------- #
# inventory scanning
# --------------------------------------------------------------------------- #


def scan_user_skills(paths: Paths) -> list[dict]:
    if not paths.skills_dir.is_dir():
        return []
    out = []
    for skill_md in sorted(paths.skills_dir.glob("*/SKILL.md")):
        fm = parse_frontmatter(skill_md)
        if not fm:
            continue
        name = _clean(fm.get("name")) or skill_md.parent.name
        out.append(_skill_record(name, fm, source="user"))
    return out


def item_key(kind: str, name: str) -> str:
    """The one identity string for an item, shared by the server and the page."""
    return f"{kind}:{name}"


def plugin_is_enabled(plugin_key: str, enabled: dict[str, bool]) -> bool:
    """A plugin absent from enabledPlugins is off - the CLI does not list its skills."""
    return bool(enabled.get(plugin_key, False))


def installed_plugins(paths: Paths) -> dict[str, Path]:
    """plugin@marketplace -> install path, from the CLI's own install record."""
    data, _ = read_json(paths.installed_plugins_file)
    out: dict[str, Path] = {}
    for key, entries in (data.get("plugins") or {}).items():
        if isinstance(entries, list) and entries:
            install_path = entries[-1].get("installPath")
            if install_path:
                out[key] = Path(install_path)
    return out


def scan_plugin_skills(plugin_key: str, install_path: Path) -> list[dict]:
    short = plugin_key.split("@", 1)[0]
    out = []
    for skill_md in sorted(install_path.glob("skills/**/SKILL.md")):
        fm = parse_frontmatter(skill_md)
        if not fm:
            continue
        bare = _clean(fm.get("name")) or skill_md.parent.name
        out.append(_skill_record(f"{short}:{bare}", fm, source=f"plugin:{plugin_key}"))
    return out


def _skill_record(name: str, fm: dict, source: str) -> dict:
    description = _clean(fm.get("description"))
    when_to_use = _clean(fm.get("whenToUse") or fm.get("when-to-use")) or None
    locked = bool(fm.get("disable-model-invocation") or fm.get("disableModelInvocation"))
    full = f"{description} - {when_to_use}" if when_to_use else description
    return {
        "kind": "skill",
        "name": name,
        "source": source,
        "description": full[:DESC_CAP],
        "lock": "author" if locked else None,
        "tokens": state_tokens(name, description, when_to_use),
    }


def scan_mcp_servers(paths: Paths, plugins: dict[str, Path], enabled: dict[str, bool]) -> list[dict]:
    claude_json, _ = read_json(paths.claude_json)
    out = [
        {"kind": "mcp", "name": name, "source": "user", "tokens": None}
        for name in sorted(claude_json.get("mcpServers") or {})
    ]
    for key, install_path in sorted(plugins.items()):
        if not plugin_is_enabled(key, enabled):
            continue
        data, _ = read_json(install_path / ".mcp.json")
        for name in sorted(data.get("mcpServers") or {}):
            out.append({"kind": "mcp", "name": name, "source": f"plugin:{key}", "tokens": None})
    return out


def usage_for(claude_json: dict, name: str) -> dict:
    entry = (claude_json.get("skillUsage") or {}).get(name) or {}
    last = entry.get("lastUsedAt")
    days = None
    if isinstance(last, int | float):
        days = max(0, int((time.time() * 1000 - last) / 86_400_000))
    return {"count": int(entry.get("usageCount") or 0), "days_since": days}


def scan_shadows(paths: Paths, claude_json: dict, user_settings_data: dict) -> dict[str, list[dict]]:
    """Project/local settings entries that *override* a user-scope choice.

    Keyed by item_key(). An entry that merely agrees with user scope is not a shadow.
    The user settings file is never treated as shadowing itself, even though $HOME may
    itself be a known project.
    """
    user_file = paths.settings_file.resolve() if paths.settings_file.exists() else paths.settings_file
    user_skills = user_settings_data.get("skillOverrides") or {}
    user_plugins = user_settings_data.get("enabledPlugins") or {}
    user_disabled_mcp = user_settings_data.get("disabledMcpjsonServers") or []
    shadows: dict[str, list[dict]] = {}

    def note(key: str, path: Path, scope: str, value: object) -> None:
        shadows.setdefault(key, []).append({"path": str(path), "scope": scope, "value": value})

    for project in claude_json.get("projects") or {}:
        for filename, scope in ((".claude/settings.json", "project"), (".claude/settings.local.json", "local")):
            path = Path(project) / filename
            if not path.exists() or path.resolve() == user_file:
                continue
            data, error = read_json(path)
            if error:
                continue
            for name, value in (data.get("skillOverrides") or {}).items():
                if value != user_skills.get(name, "on"):
                    note(item_key("skill", name), path, scope, value)
            for name, value in (data.get("enabledPlugins") or {}).items():
                if bool(value) != plugin_is_enabled(name, user_plugins):
                    note(item_key("plugin", name), path, scope, bool(value))
            for name in data.get("disabledMcpjsonServers") or []:
                if name not in user_disabled_mcp:
                    note(item_key("mcp", name), path, scope, False)
    return shadows


def build_state(paths: Paths, context_window: int = DEFAULT_CONTEXT_WINDOW,
                undo_available: bool = False) -> dict:
    settings, settings_error = read_json(paths.settings_file)
    claude_json, data_error = read_json(paths.claude_json)

    overrides = settings.get("skillOverrides") or {}
    enabled_plugins = settings.get("enabledPlugins") or {}
    disabled_mcp = settings.get("disabledMcpjsonServers") or []
    bundled_killed = bool(settings.get("disableBundledSkills"))
    plugins = installed_plugins(paths)
    shadows = scan_shadows(paths, claude_json, settings)

    skills: list[dict] = scan_user_skills(paths)
    for key, install_path in sorted(plugins.items()):
        if plugin_is_enabled(key, enabled_plugins):
            skills.extend(scan_plugin_skills(key, install_path))

    bundled = extract_bundled_skills(paths)
    skills.extend(
        _skill_record(name, {"name": name, "description": description}, source="bundled")
        for name, description in bundled
    )

    for skill in skills:
        locked = skill["lock"] == "author"
        raw = overrides.get(skill["name"])
        if bundled_killed and skill["source"] == "bundled":
            state = "off"  # the master switch already removed them from context
        elif locked:
            state = "off" if raw == "off" else "user-invocable-only"
        else:
            state = raw if raw in SKILL_STATES else "on"
        skill["state"] = state
        skill["allowed_states"] = list(LOCKED_STATES if locked else SKILL_STATES)
        skill["usage"] = usage_for(claude_json, skill["name"])
        skill["shadows"] = shadows.get(item_key("skill", skill["name"]), [])

    tokens_by_plugin: defaultdict[str, int] = defaultdict(int)
    counts_by_plugin: defaultdict[str, int] = defaultdict(int)
    for skill in skills:
        if skill["source"].startswith("plugin:"):
            key = skill["source"].split(":", 1)[1]
            tokens_by_plugin[key] += skill["tokens"][skill["state"]]
            counts_by_plugin[key] += 1

    mcp = scan_mcp_servers(paths, plugins, enabled_plugins)
    for server in mcp:
        server["state"] = "off" if server["name"] in disabled_mcp else "on"
        server["shadows"] = shadows.get(item_key("mcp", server["name"]), [])

    plugin_items = []
    for key in sorted(set(plugins) | set(enabled_plugins)):
        on = plugin_is_enabled(key, enabled_plugins)
        plugin_items.append(
            {
                "kind": "plugin",
                "name": key,
                "source": "plugin",
                "state": "on" if on else "off",
                "skill_count": counts_by_plugin.get(key, 0),
                "tokens_always_paid": tokens_by_plugin.get(key, 0),
                "mcp_server_count": sum(1 for s in mcp if s["source"] == f"plugin:{key}"),
                "shadows": shadows.get(item_key("plugin", key), []),
            }
        )

    # The master kill switch is the documented fallback for when extraction fails, so it is
    # only offered then - otherwise the individual bundled rows are the way to trim.
    master_rows: list[dict] = []
    if not bundled:
        master_rows.append(
            {
                "kind": "bundled-master",
                "name": "disableBundledSkills",
                "source": "bundled",
                "state": "off" if bundled_killed else "on",
                "shadows": [],
            }
        )

    # Bundled gating cannot be verified offline (the CLI decides at runtime), so those
    # rows are subtotalled separately and never move the headline savings number.
    always_paid = sum(s["tokens"][s["state"]] for s in skills if s["source"] != "bundled")
    bundled_detected = sum(s["tokens"][s["state"]] for s in skills if s["source"] == "bundled")
    budget_tokens = int(context_window * BUDGET_FRACTION)
    listing_tokens = always_paid + bundled_detected

    return {
        "items": skills + plugin_items + mcp + master_rows,
        "meters": {
            "always_paid": always_paid,
            "bundled_detected": bundled_detected,
            "budget": {
                "context_window": context_window,
                "tokens": budget_tokens,
                "listing_tokens": listing_tokens,
                "over_by": max(0, listing_tokens - budget_tokens),
                "protected_tokens": bundled_detected,
                "competing_budget": budget_tokens - bundled_detected,
            },
            "on_demand": {
                "servers_enabled": sum(1 for s in mcp if s["state"] == "on"),
                "servers_total": len(mcp),
                "measured": False,
            },
        },
        "bundled": {
            "extracted": bool(bundled),
            "count": len(bundled),
            "note": (
                f"{len(bundled)} bundled skills detected in the CLI build. Gating is decided at "
                "runtime and cannot be read offline, so these are subtotalled separately - some "
                "rows may be slash commands rather than listed skills. Toggling them still writes "
                "a real override."
                if bundled
                else "Could not read bundled skills from this CLI build - use the master toggle."
            ),
        },
        "settings_error": settings_error,
        "data_error": data_error,
        "settings_file": str(paths.settings_file),
        "undo_available": undo_available,
        "stale_days": STALE_DAYS,
    }


# --------------------------------------------------------------------------- #
# apply / undo
# --------------------------------------------------------------------------- #


class ApplyError(Exception):
    """A change set that must not be written."""


def _write_atomically(path: Path, data: dict) -> None:
    """Write via a same-directory temp file, fsync, then rename - so a crash cannot
    leave a half-written settings file. The original file's mode is preserved."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".context-saver-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _dict_diff(key: str, before: dict, after: dict) -> dict:
    changed = {}
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            changed[name] = {"before": before.get(name), "after": after.get(name)}
    return {key: changed} if changed else {}


def apply_changes(
    paths: Paths, changes: list[dict], preview: bool = False, stack: UndoStack | None = None
) -> dict:
    """Compute the diff for `changes`; write it unless `preview`.

    The page always previews first, so the diff the user confirms is the diff that lands.
    """
    settings, error = read_json(paths.settings_file)
    if error:
        raise ApplyError(f"refusing to write: user settings could not be parsed ({error})")

    state = build_state(paths)
    by_key = {item_key(i["kind"], i["name"]): i for i in state["items"]}

    overrides = dict(settings.get("skillOverrides") or {})
    plugins = dict(settings.get("enabledPlugins") or {})
    disabled_mcp = list(settings.get("disabledMcpjsonServers") or [])
    bundled_disabled = bool(settings.get("disableBundledSkills"))

    for change in changes:
        kind, name = change.get("kind"), change.get("name")
        if kind == "bundled-master":
            if name != "disableBundledSkills":
                raise ApplyError(f"unknown bundled master toggle: {name!r}")
            bundled_disabled = not bool(change.get("enabled", True))
            continue

        item = by_key.get(item_key(kind or "", name or ""))
        if item is None:
            raise ApplyError(f"unknown {kind or 'item'}: {name!r}")

        if kind == "skill":
            new_state = change.get("state")
            if new_state not in SKILL_STATES:
                raise ApplyError(f"invalid state {new_state!r} for skill {name!r}")
            if new_state not in item["allowed_states"]:
                raise ApplyError(
                    f"skill {name!r} is locked by its author (disable-model-invocation); "
                    f"allowed states: {', '.join(item['allowed_states'])}"
                )
            if new_state == "on" and item["lock"] is None:
                overrides.pop(name, None)
            else:
                overrides[name] = new_state
        elif kind == "plugin":
            wanted = bool(change.get("enabled", True))
            # absent from enabledPlugins already means off, so don't write a redundant false
            if not wanted and name not in plugins:
                continue
            plugins[name] = wanted
        elif kind == "mcp":
            if change.get("enabled", True):
                disabled_mcp = [s for s in disabled_mcp if s != name]
            elif name not in disabled_mcp:
                disabled_mcp.append(name)
        else:
            raise ApplyError(f"unknown item kind: {kind!r}")

    updated = dict(settings)
    diff: dict[str, Any] = {}
    diff |= _dict_diff("skillOverrides", settings.get("skillOverrides") or {}, overrides)
    diff |= _dict_diff("enabledPlugins", settings.get("enabledPlugins") or {}, plugins)

    before_mcp = list(settings.get("disabledMcpjsonServers") or [])
    if sorted(before_mcp) != sorted(disabled_mcp):
        diff["disabledMcpjsonServers"] = {"before": before_mcp, "after": sorted(disabled_mcp)}
    if bool(settings.get("disableBundledSkills")) != bundled_disabled:
        diff["disableBundledSkills"] = {
            "before": settings.get("disableBundledSkills"),
            "after": bundled_disabled,
        }

    if preview or not diff:
        return {
            "diff": diff,
            "backup": None,
            "preview": bool(preview),
            "settings_file": str(paths.settings_file),
        }

    _set_or_drop(updated, "skillOverrides", overrides)
    _set_or_drop(updated, "enabledPlugins", plugins)
    _set_or_drop(updated, "disabledMcpjsonServers", sorted(disabled_mcp))
    if bundled_disabled:
        updated["disableBundledSkills"] = True
    else:
        updated.pop("disableBundledSkills", None)

    backup = _make_backup(paths)
    _write_atomically(paths.settings_file, updated)
    if stack is not None:
        stack.push(backup)
    return {"diff": diff, "backup": str(backup) if backup else None,
            "settings_file": str(paths.settings_file)}


def _set_or_drop(settings: dict, key: str, value: dict | list) -> None:
    if value:
        settings[key] = value
    else:
        settings.pop(key, None)


def _make_backup(paths: Paths) -> Path | None:
    if not paths.settings_file.exists():
        return None
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    target = paths.backups_dir / f"{BACKUP_PREFIX}{time.time_ns()}.json"
    shutil.copy2(paths.settings_file, target)
    return target


class UndoStack:
    """Backups written by *this* run, newest last.

    Scoped to the session on purpose: "undo last apply" must not resurrect a file left
    behind by a run days ago, which is what picking the newest backup on disk would do.
    """

    def __init__(self) -> None:
        self._backups: list[Path] = []

    def push(self, backup: Path | None) -> None:
        if backup is not None:
            self._backups.append(backup)

    def pop(self) -> Path | None:
        return self._backups.pop() if self._backups else None

    def __bool__(self) -> bool:
        return bool(self._backups)


def undo(paths: Paths, stack: UndoStack) -> dict:
    backup = stack.pop()
    if backup is None:
        raise ApplyError("nothing to undo: no apply has been made in this session")

    saved, error = read_json(backup)
    if error:
        raise ApplyError(f"refusing to undo: backup could not be parsed ({error})")
    current, current_error = read_json(paths.settings_file)
    if current_error:
        raise ApplyError(f"refusing to undo: user settings could not be parsed ({current_error})")

    restored = dict(current)
    diff: dict[str, Any] = {}
    for key in MANAGED_KEYS:
        before, after = current.get(key), saved.get(key)
        if before != after:
            diff[key] = {"before": before, "after": after}
        if after in (None, {}, [], False):
            restored.pop(key, None)
        else:
            restored[key] = after

    _write_atomically(paths.settings_file, restored)
    backup.rename(backup.with_suffix(".json.undone"))
    return {"diff": diff, "restored_from": str(backup), "settings_file": str(paths.settings_file)}


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #


def _origin_is_local(origin: str | None) -> bool:
    """Allow same-machine origins only.

    A page on any website can POST to a localhost server; without this check a drive-by
    request could rewrite settings or fire an undo. A missing Origin is not a browser
    cross-site request (curl, the test client), so it is allowed.
    """
    if not origin:
        return True
    host = urlsplit(origin).hostname
    return host in ("127.0.0.1", "localhost", "::1")


def make_server(paths: Paths, port: int = 8787,
                context_window: int = DEFAULT_CONTEXT_WINDOW) -> ThreadingHTTPServer:
    undo_stack = UndoStack()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            route, _, query = self.path.partition("?")
            if route == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif route == "/api/state":
                window = parse_qs(query).get("context_window", [""])[0]
                self._json(
                    200,
                    build_state(
                        paths,
                        context_window=int(window) if window.isdigit() else context_window,
                        undo_available=bool(undo_stack),
                    ),
                )
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not _origin_is_local(self.headers.get("Origin")):
                self._json(403, {"error": "cross-site Origin refused"})
                return
            # JSON-only: blocks the simple cross-site requests browsers send unpreflighted
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type != "application/json":
                self._json(415, {"error": "Content-Type must be application/json"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"invalid JSON body: {exc}"})
                return

            route = self.path.split("?")[0]
            try:
                if route == "/api/apply":
                    changes = body.get("changes")
                    if not isinstance(changes, list):
                        raise ApplyError("`changes` must be a list")
                    self._json(
                        200,
                        apply_changes(
                            paths, changes, preview=bool(body.get("preview")), stack=undo_stack
                        ),
                    )
                elif route == "/api/undo":
                    self._json(200, undo(paths, undo_stack))
                else:
                    self._json(404, {"error": "not found"})
            except ApplyError as exc:
                self._json(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Context Saver</title><style>
:root{color-scheme:light dark;--bg:#0f1115;--fg:#e6e6e6;--dim:#9aa0a6;--card:#171a21;--line:#262b33;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#60a5fa}
@media(prefers-color-scheme:light){:root{--bg:#f7f8fa;--fg:#1a1d23;--dim:#5f6672;--card:#fff;--line:#e3e6eb}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:14px 20px}
h1{font-size:16px;margin:0 0 10px}
.meters{display:flex;gap:20px;flex-wrap:wrap;align-items:flex-end}
.meter{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:210px}
.meter .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.meter .val{font-size:22px;font-weight:600}
.save{color:var(--ok)}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent)}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;
padding:6px 11px;cursor:pointer;font:inherit}
button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:#08101c;font-weight:600}
input[type=search]{background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:6px 10px;font:inherit;min-width:200px}
.banner{background:color-mix(in srgb,var(--warn) 14%,transparent);border:1px solid var(--warn);
border-radius:6px;padding:7px 11px;margin-top:10px;font-size:13px}
main{padding:16px 20px 80px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);
padding:6px 8px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr.staged{background:color-mix(in srgb,var(--accent) 10%,transparent)}
.name{font-weight:600}.desc{color:var(--dim);font-size:12px;max-width:640px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.src{font-size:11px;color:var(--dim);white-space:nowrap}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;border-radius:0;padding:3px 8px;font-size:11px;background:transparent}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed=true]{background:var(--accent);color:#08101c;font-weight:600}
.seg button:disabled{opacity:.3}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{font-size:10px;padding:1px 6px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.shadow{color:var(--warn);font-size:11px}
.sec{margin:26px 0 8px;font-size:13px;font-weight:600;display:flex;gap:8px;align-items:baseline}
.sec span{font-weight:400;color:var(--dim);font-size:12px}
dialog{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;
max-width:720px;width:92%;padding:18px}
dialog::backdrop{background:#0008}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:11px;
overflow:auto;max-height:50vh;font-size:12px}
footer{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--line);
padding:10px 20px;display:flex;gap:10px;align-items:center}
.grow{flex:1}.err{color:var(--bad)}
</style></head><body>
<header>
  <h1>Context Saver <span class="pill" id="settings-path"></span></h1>
  <div class="meters">
    <div class="meter"><div class="lbl">Always-paid context</div>
      <div class="val"><span id="ap-now">-</span> <span class="save" id="ap-save"></span></div>
      <div class="bar"><i id="ap-bar"></i></div>
      <div class="lbl" id="ap-note">skill descriptions, every turn</div></div>
    <div class="meter"><div class="lbl">Listing budget (1% of window)</div>
      <div class="val"><span id="bg-now">-</span><span class="lbl"> / <span id="bg-cap">-</span></span></div>
      <div class="bar"><i id="bg-bar"></i></div>
      <div class="lbl" id="bg-note"></div></div>
    <div class="meter"><div class="lbl">Bundled (unverified)</div>
      <div class="val"><span id="bd-now">-</span> <span class="save" id="bd-save"></span></div>
      <div class="lbl">built into the CLI &middot; gating not readable offline</div></div>
    <div class="meter"><div class="lbl">On-demand exposure</div>
      <div class="val" id="od-now">-</div>
      <div class="lbl">MCP servers &middot; schemas load on use, not per turn</div></div>
  </div>
  <div class="controls">
    <input type="search" id="q" placeholder="filter by name or description">
    <button id="suggest">Select never-used &amp; stale</button>
    <button id="reset">Clear staged</button>
    <span class="grow"></span>
    <label><input type="checkbox" id="hide-off"> hide already-off</label>
  </div>
  <div class="banner">Changes apply to <b>new</b> sessions. Writes go to user scope only.</div>
  <div class="banner err" id="settings-error" hidden></div>
</header>
<main>
  <div class="sec">Skills <span id="skill-note"></span></div>
  <table><thead><tr>
    <th data-sort="name">Skill</th><th data-sort="source">Source</th>
    <th data-sort="tokens" class="num">Tokens</th><th data-sort="usage" class="num">Used</th>
    <th data-sort="stale" class="num">Last</th><th>State</th>
  </tr></thead><tbody id="skills"></tbody></table>

  <div class="sec">Plugins <span>toggling a plugin removes everything it contributes</span></div>
  <table><thead><tr>
    <th data-sort="name">Plugin</th><th class="num">Skills</th><th class="num">Tokens</th>
    <th class="num">MCP</th><th>State</th>
  </tr></thead><tbody id="plugins"></tbody></table>

  <div class="sec">MCP servers <span>hygiene, not per-turn savings</span></div>
  <table><thead><tr>
    <th data-sort="name">Server</th><th data-sort="source">Source</th><th>State</th>
  </tr></thead><tbody id="mcp"></tbody></table>
</main>
<footer>
  <span id="staged-count">0 staged</span><span class="grow"></span>
  <button id="undo">Undo last apply</button>
  <button class="primary" id="apply" disabled>Review &amp; apply</button>
</footer>
<dialog id="dlg"><h3 id="dlg-title">Changes to write</h3><pre id="dlg-body"></pre>
  <div class="controls"><span class="grow"></span>
    <button id="dlg-cancel">Cancel</button>
    <button class="primary" id="dlg-ok">Write to settings.json</button></div></dialog>
<script>
const STATES = ["on","name-only","user-invocable-only","off"];
const LABEL = {"on":"on","name-only":"name","user-invocable-only":"user","off":"off"};
let state = null, staged = new Map(), sortKey = "tokens", sortDir = -1;

const fmt = n => n >= 1000 ? (n/1000).toFixed(1)+"k" : String(n);
const key = it => it.kind + ":" + it.name;
// Names, sources and shadow values come from files on disk, so every interpolation
// below is escaped - never trust a skill's frontmatter or a project's settings.
const esc = v => String(v ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stagedState = it => staged.has(key(it)) ? staged.get(key(it)) : it.state;

async function load(){
  state = await (await fetch("/api/state")).json();
  staged.clear();
  document.getElementById("settings-path").textContent = state.settings_file;
  const err = document.getElementById("settings-error");
  const problem = state.settings_error
    ? "Settings unreadable - applying is disabled: " + state.settings_error
    : state.data_error ? "Usage and MCP data unavailable: " + state.data_error : "";
  err.hidden = !problem;
  err.textContent = problem;
  document.getElementById("skill-note").textContent = state.bundled.note;
  document.getElementById("undo").disabled = !state.undo_available;
  render();
}

function items(kind){ return state.items.filter(i => i.kind === kind); }

function tokensFor(it, st){ return it.tokens ? (it.tokens[st] ?? 0) : 0; }

function render(){
  const q = document.getElementById("q").value.toLowerCase();
  const hideOff = document.getElementById("hide-off").checked;
  let skills = items("skill").filter(it =>
    (!q || it.name.toLowerCase().includes(q) || (it.description||"").toLowerCase().includes(q)) &&
    (!hideOff || stagedState(it) !== "off"));

  const cmp = {
    name: it => it.name.toLowerCase(),
    source: it => it.source,
    tokens: it => tokensFor(it, stagedState(it)),
    usage: it => it.usage.count,
    stale: it => it.usage.days_since === null ? 1e9 : it.usage.days_since,
  }[sortKey] || (it => it.name);
  skills.sort((a,b) => { const x=cmp(a), y=cmp(b); return (x>y?1:x<y?-1:0) * sortDir; });

  document.getElementById("skills").innerHTML = skills.map(it => {
    const st = stagedState(it);
    const seg = STATES.map(s => `<button data-k="${esc(key(it))}" data-s="${s}"
      aria-pressed="${s===st}" ${it.allowed_states.includes(s)?"":"disabled"}
      title="${s} - ${tokensFor(it,s)} tokens">${LABEL[s]}</button>`).join("");
    const used = it.usage.count ? it.usage.count+"&times;" : "<span class=dim>never</span>";
    const last = it.usage.days_since === null ? "-" : it.usage.days_since + "d";
    const shadow = it.shadows.length
      ? `<div class="shadow" title="${esc(it.shadows.map(s=>s.path).join(" | "))}">shadowed by ${esc(it.shadows[0].scope)} settings &rarr; ${esc(it.shadows[0].value)}</div>` : "";
    // the cost shown is what this row actually pays at its staged state, so an
    // author-locked or already-off skill honestly reads 0
    return `<tr class="${staged.has(key(it))?"staged":""}">
      <td><div class="name">${esc(it.name)} ${it.lock?'<span class="pill">author-locked</span>':""}</div>
          <div class="desc">${esc(it.description)}</div>${shadow}</td>
      <td class="src">${esc(it.source)}</td>
      <td class="num" title="${tokensFor(it,"on")} tokens at full description">${tokensFor(it,st)}</td>
      <td class="num">${used}</td><td class="num">${last}</td>
      <td><div class="seg">${seg}</div></td></tr>`;
  }).join("");

  const onOff = it => { const on = stagedState(it) === "on"; return `<div class="seg">
      <button data-k="${esc(key(it))}" data-s="on" aria-pressed="${on}">on</button>
      <button data-k="${esc(key(it))}" data-s="off" aria-pressed="${!on}">off</button></div>`; };

  document.getElementById("plugins").innerHTML = items("plugin").map(it => {
    const shadow = it.shadows.length
      ? `<div class="shadow">shadowed by ${esc(it.shadows[0].scope)} settings &rarr; ${esc(it.shadows[0].value)}</div>` : "";
    return `<tr class="${staged.has(key(it))?"staged":""}">
      <td><div class="name">${esc(it.name)}</div>${shadow}</td>
      <td class="num">${it.skill_count}</td><td class="num">${it.tokens_always_paid}</td>
      <td class="num">${it.mcp_server_count}</td>
      <td>${onOff(it)}</td></tr>`;
  }).join("");

  const mcpRows = items("mcp").concat(items("bundled-master"));
  document.getElementById("mcp").innerHTML = mcpRows.map(it => {
    const label = it.kind === "bundled-master" ? "bundled skills (master switch)" : it.name;
    return `<tr class="${staged.has(key(it))?"staged":""}">
      <td><div class="name">${esc(label)}</div></td><td class="src">${esc(it.source)}</td>
      <td>${onOff(it)}</td></tr>`;
  }).join("");

  const sum = (list, pick) => list.reduce((n,it) => n + tokensFor(it, pick(it)), 0);
  const verified = items("skill").filter(it => it.source !== "bundled");
  const bundledRows = items("skill").filter(it => it.source === "bundled");
  const baseline = sum(verified, it => it.state), now = sum(verified, stagedState);
  document.getElementById("ap-now").textContent = fmt(now);
  document.getElementById("ap-save").textContent = now < baseline ? `(-${fmt(baseline-now)})` : "";
  document.getElementById("ap-bar").style.width = baseline ? (100*now/baseline)+"%" : "0%";
  const bBase = sum(bundledRows, it => it.state), bNow = sum(bundledRows, stagedState);
  document.getElementById("bd-now").textContent = fmt(bNow);
  document.getElementById("bd-save").textContent = bNow < bBase ? `(-${fmt(bBase-bNow)})` : "";

  // The listing is capped at 1% of the context window. While you are over the cap the
  // CLI already truncates the tail, so trimming buys back descriptions, not tokens.
  const bg = state.meters.budget, listing = now + bNow;
  document.getElementById("bg-now").textContent = fmt(listing);
  document.getElementById("bg-cap").textContent = fmt(bg.tokens);
  const barEl = document.getElementById("bg-bar");
  barEl.style.width = Math.min(100, 100*listing/bg.tokens) + "%";
  barEl.style.background = listing > bg.tokens ? "var(--warn)" : "var(--ok)";
  document.getElementById("bg-note").textContent = listing > bg.tokens
    ? `over by ${fmt(listing-bg.tokens)} - the tail is truncated to names; trimming restores descriptions first`
    : `under the cap - every token you trim is a real per-turn saving`;
  document.getElementById("ap-note").textContent = listing > bg.tokens
    ? "skill descriptions (uncapped) - see budget" : "skill descriptions, every turn";
  const od = state.meters.on_demand;
  document.getElementById("od-now").textContent =
    `${items("mcp").filter(i=>stagedState(i)==="on").length}/${od.servers_total} servers`;
  document.getElementById("staged-count").textContent = staged.size + " staged";
  document.getElementById("apply").disabled = staged.size === 0 || !!state.settings_error;
}

document.addEventListener("click", e => {
  const b = e.target.closest("button[data-k]");
  if(!b) return;
  const it = state.items.find(i => key(i) === b.dataset.k);
  if(b.dataset.s === it.state) staged.delete(b.dataset.k);
  else staged.set(b.dataset.k, b.dataset.s);
  render();
});
document.querySelectorAll("th[data-sort]").forEach(th => th.onclick = () => {
  const k = th.dataset.sort;
  sortDir = (k === sortKey) ? -sortDir : (k === "name" || k === "source" ? 1 : -1);
  sortKey = k; render();
});
document.getElementById("q").oninput = render;
document.getElementById("hide-off").onchange = render;
document.getElementById("reset").onclick = () => { staged.clear(); render(); };
document.getElementById("suggest").onclick = () => {
  for(const it of items("skill")){
    const never = it.usage.count === 0;
    const stale = it.usage.days_since !== null && it.usage.days_since > state.stale_days;
    if((never || stale) && it.state !== "off" && it.allowed_states.includes("user-invocable-only"))
      staged.set(key(it), "user-invocable-only");
  }
  render();
};

const dlg = document.getElementById("dlg");
function changeList(){
  return [...staged.entries()].map(([k, s]) => {
    const it = state.items.find(i => key(i) === k);
    return it.kind === "skill" ? {kind:"skill", name:it.name, state:s}
                               : {kind:it.kind, name:it.name, enabled: s === "on"};
  });
}
const post = (path, body) => fetch(path, {method:"POST",
  headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});

// Preview first: the diff you confirm is the diff that gets written.
document.getElementById("apply").onclick = async () => {
  const changes = changeList();
  const res = await post("/api/apply", {changes, preview: true});
  const data = await res.json();
  if(!res.ok){ alert(data.error); await load(); return; }
  if(!Object.keys(data.diff).length){ alert("Nothing to write - staged values match your settings."); return; }

  document.getElementById("dlg-title").textContent = "Review changes to " + state.settings_file;
  document.getElementById("dlg-body").textContent = JSON.stringify(data.diff, null, 2);
  const ok = document.getElementById("dlg-ok");
  ok.hidden = false;
  ok.onclick = async () => {
    const applied = await post("/api/apply", {changes});
    const result = await applied.json();
    dlg.close();
    if(!applied.ok) alert(result.error);
    else if(result.backup) console.info("backup:", result.backup);
    await load();
  };
  dlg.showModal();
};
document.getElementById("dlg-cancel").onclick = () => dlg.close();
document.getElementById("undo").onclick = async () => {
  const res = await post("/api/undo", {});
  const data = await res.json();
  if(!res.ok) alert(data.error);
  await load();
};
load();
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", default=os.environ.get("CONTEXT_SAVER_HOME", str(Path.home())),
                        help="root holding .claude/ and .claude.json (default: your home directory)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW,
                        help="context window the listing budget is 1%% of (default: 1000000)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    paths = Paths(home=Path(args.home).expanduser())
    server = make_server(paths, args.port, context_window=args.context_window)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Context Saver -> {url}   (managing {paths.settings_file})")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
