"""All behaviour is driven through the HTTP API against a fake CLAUDE_HOME."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import items_by_name, write_json


def settings(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text())


# --------------------------------------------------------------------------- #
# preview before write
# --------------------------------------------------------------------------- #


def test_preview_returns_the_diff_without_writing_anything(client, home):
    before = settings(home)
    result = client.post(
        "/api/apply",
        {"preview": True, "changes": [{"kind": "skill", "name": "never-used", "state": "off"}]},
    )

    assert result["preview"] is True
    assert result["diff"]["skillOverrides"]["never-used"] == {"before": None, "after": "off"}
    assert result["backup"] is None
    assert settings(home) == before  # nothing written
    assert not (home / ".claude" / "backups").exists()


def test_preview_rejects_a_forbidden_change_before_writing(client, home):
    before = settings(home)
    code, body = client.post_expect_error(
        "/api/apply",
        {"preview": True, "changes": [{"kind": "skill", "name": "locked-skill", "state": "on"}]},
    )
    assert code == 400
    assert "locked-skill" in body["error"]
    assert settings(home) == before


def test_confirming_after_a_preview_writes_the_same_diff(client, home):
    changes = [{"kind": "skill", "name": "never-used", "state": "off"}]
    preview = client.post("/api/apply", {"preview": True, "changes": changes})
    applied = client.post("/api/apply", {"changes": changes})

    assert applied["diff"] == preview["diff"]
    assert applied.get("preview") is not True
    assert settings(home)["skillOverrides"]["never-used"] == "off"


# --------------------------------------------------------------------------- #
# request safety
# --------------------------------------------------------------------------- #


def test_a_cross_site_origin_cannot_write(client, home):
    before = settings(home)
    code, body = client.post_expect_error(
        "/api/apply",
        {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]},
        headers={"Origin": "http://evil.example"},
    )
    assert code == 403
    assert "origin" in body["error"].lower()
    assert settings(home) == before


def test_a_localhost_origin_may_write(client, home):
    client.post(
        "/api/apply",
        {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]},
        headers={"Origin": client.base},
    )
    assert settings(home)["skillOverrides"]["never-used"] == "off"


def test_a_form_style_content_type_cannot_write(client, home):
    """Blocks simple cross-site requests, which browsers send without a preflight."""
    before = settings(home)
    code, _ = client.post_expect_error(
        "/api/apply",
        {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]},
        headers={"Content-Type": "text/plain"},
    )
    assert code == 415
    assert settings(home) == before


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #


def test_lists_user_skills_with_per_state_token_costs(client):
    skills = items_by_name(client.get("/api/state"), "skill")

    driver = skills["daily-driver"]
    assert driver["source"] == "user"
    assert driver["state"] == "on"
    # `- daily-driver: A skill used constantly.` -> 40 chars -> 10 tokens
    assert driver["tokens"]["on"] == 10
    # `- daily-driver` -> 14 chars -> 4 tokens
    assert driver["tokens"]["name-only"] == 4
    assert driver["tokens"]["user-invocable-only"] == 0
    assert driver["tokens"]["off"] == 0


def test_when_to_use_is_part_of_the_injected_description(client):
    skills = items_by_name(client.get("/api/state"), "skill")
    chisel = skills["toolkit:chisel"]
    assert chisel["tokens"]["on"] > skills["toolkit:hammer"]["tokens"]["on"]
    assert "Use when carving is required." in chisel["description"]


def test_plugin_skills_are_listed_qualified_and_only_for_enabled_plugins(client):
    skills = items_by_name(client.get("/api/state"), "skill")
    assert "toolkit:hammer" in skills
    assert skills["toolkit:hammer"]["source"] == "plugin:toolkit@market"
    # skills of a disabled plugin cost nothing and must not be listed
    assert not [n for n in skills if n.startswith("offkit:") or n == "ghost"]


def test_existing_user_override_is_reported_as_current_state(client):
    skills = items_by_name(client.get("/api/state"), "skill")
    assert skills["stale-skill"]["state"] == "off"


def test_author_locked_skill_reports_lock_and_allowed_states(client):
    locked = items_by_name(client.get("/api/state"), "skill")["locked-skill"]
    assert locked["lock"] == "author"
    assert locked["state"] == "user-invocable-only"
    assert locked["allowed_states"] == ["user-invocable-only", "off"]


def test_unlocked_skill_allows_all_four_states(client):
    driver = items_by_name(client.get("/api/state"), "skill")["daily-driver"]
    assert driver["allowed_states"] == ["on", "name-only", "user-invocable-only", "off"]


def test_bundled_skills_are_extracted_from_the_cli_binary(client):
    skills = items_by_name(client.get("/api/state"), "skill")
    assert skills["fake-dataviz"]["source"] == "bundled"
    # description was indirected through a minified variable and still resolved
    assert "Bundled charting guidance" in skills["fake-dataviz"]["description"]
    assert "Repeat a prompt on a schedule." in skills["fake-loop"]["description"]
    assert client.get("/api/state")["bundled"]["extracted"] is True


def test_bundled_extraction_failure_falls_back_to_master_toggle(client, home):
    binary = home / ".local" / "share" / "claude" / "versions" / "9.9.9"
    binary.write_text("a CLI whose skill registration format we no longer recognise")

    state = client.get("/api/state")
    assert state["bundled"]["extracted"] is False
    assert not [i for i in state["items"] if i["source"] == "bundled" and i["kind"] == "skill"]
    master = items_by_name(state, "bundled-master")
    assert "disableBundledSkills" in master


def test_bundled_extraction_is_cached_and_reruns_for_a_new_cli_build(client, home):
    first = items_by_name(client.get("/api/state"), "skill")
    assert "fake-dataviz" in first

    cache = home / ".claude" / "context-saver" / "bundled.json"
    assert cache.exists()  # scanning a 271MB CLI build is slow; it must be cached

    # a new build with different content must not serve the stale cache
    binary = home / ".local" / "share" / "claude" / "versions" / "9.9.9"
    binary.write_text(
        'var z1="Replaced bundled skill.";\nfunction q(){nu({name:"fake-replacement",'
        'menuDescription:"m",description:z1,userInvocable:!0})}\n'
    )
    second = items_by_name(client.get("/api/state"), "skill")
    assert "fake-replacement" in second
    assert "fake-dataviz" not in second


def test_usage_data_is_reported_including_never_used(client):
    skills = items_by_name(client.get("/api/state"), "skill")
    assert skills["daily-driver"]["usage"]["count"] == 27
    assert skills["daily-driver"]["usage"]["days_since"] == 1
    assert skills["stale-skill"]["usage"]["days_since"] == 90
    assert skills["never-used"]["usage"]["count"] == 0
    assert skills["never-used"]["usage"]["days_since"] is None


def test_plugins_and_mcp_servers_are_listed_with_state(client):
    state = client.get("/api/state")
    plugins = items_by_name(state, "plugin")
    assert plugins["toolkit@market"]["state"] == "on"
    assert plugins["offkit@market"]["state"] == "off"
    # a plugin carries the always-paid cost of the skills it contributes
    assert plugins["toolkit@market"]["skill_count"] == 2
    assert plugins["toolkit@market"]["tokens_always_paid"] > 0

    mcp = items_by_name(state, "mcp")
    assert mcp["db"]["state"] == "on"
    assert mcp["db"]["source"] == "user"
    assert mcp["toolkit-api"]["source"] == "plugin:toolkit@market"


def test_mcp_rows_carry_no_invented_token_cost(client):
    mcp = items_by_name(client.get("/api/state"), "mcp")
    assert mcp["db"]["tokens"] is None


def test_installed_plugin_absent_from_settings_is_treated_as_disabled(client):
    state = client.get("/api/state")
    # /context never lists skills of an installed-but-not-enabled plugin, so nor do we
    assert "straykit:drifter" not in items_by_name(state, "skill")
    assert items_by_name(state, "plugin")["straykit@market"]["state"] == "off"


def test_meters_separate_always_paid_from_on_demand(client):
    state = client.get("/api/state")
    meters = state["meters"]
    skills = items_by_name(state, "skill")

    verified = [s for s in skills.values() if s["source"] != "bundled"]
    assert meters["always_paid"] == sum(s["tokens"][s["state"]] for s in verified)
    assert meters["on_demand"]["servers_enabled"] == 3
    assert meters["on_demand"]["measured"] is False


def test_budget_reflects_the_cli_listing_cap(client):
    """The CLI caps the listing at 1% of the context window; bundled entries are protected."""
    state = client.get("/api/state")
    budget = state["meters"]["budget"]
    meters = state["meters"]

    assert budget["context_window"] == 1_000_000
    assert budget["tokens"] == 10_000
    assert budget["listing_tokens"] == meters["always_paid"] + meters["bundled_detected"]
    assert budget["protected_tokens"] == meters["bundled_detected"]
    assert budget["competing_budget"] == 10_000 - meters["bundled_detected"]
    # the fixture is nowhere near the cap
    assert budget["over_by"] == 0


def test_budget_reports_the_overshoot_for_a_smaller_context_window(client, home):
    """The window is a request parameter, not an invented key in the user's settings."""
    budget = client.get("/api/state?context_window=1000")["meters"]["budget"]
    assert budget["tokens"] == 10
    assert budget["over_by"] == budget["listing_tokens"] - 10
    assert budget["over_by"] > 0
    assert "contextSaverContextWindow" not in settings(home)


def test_the_bundled_master_switch_zeroes_the_bundled_subtotal(client):
    """With bundled skills killed, their cost is gone - the subtotal must say so."""
    assert client.get("/api/state")["meters"]["bundled_detected"] > 0
    client.post(
        "/api/apply",
        {"changes": [{"kind": "bundled-master", "name": "disableBundledSkills", "enabled": False}]},
    )

    state = client.get("/api/state")
    assert state["meters"]["bundled_detected"] == 0
    assert all(s["state"] == "off" for s in items_by_name(state, "skill").values() if s["source"] == "bundled")


def test_bundled_skills_are_subtotalled_separately_from_the_headline(client):
    """Bundled gating cannot be verified offline, so it must not move the headline number."""
    state = client.get("/api/state")
    bundled = [s for s in items_by_name(state, "skill").values() if s["source"] == "bundled"]

    assert bundled  # extraction worked in this fixture
    assert state["meters"]["bundled_detected"] == sum(s["tokens"][s["state"]] for s in bundled)
    assert state["meters"]["bundled_detected"] > 0
    assert state["meters"]["always_paid"] < state["meters"]["always_paid"] + state["meters"]["bundled_detected"]


# --------------------------------------------------------------------------- #
# shadow detection
# --------------------------------------------------------------------------- #


def test_shadowing_project_and_local_settings_are_reported(client, tmp_path):
    state = client.get("/api/state")
    driver = items_by_name(state, "skill")["daily-driver"]
    assert driver["shadows"] == [
        {
            "path": str(tmp_path / "proj-shadow" / ".claude" / "settings.local.json"),
            "scope": "local",
            "value": "off",
        }
    ]
    toolkit = items_by_name(state, "plugin")["toolkit@market"]
    assert toolkit["shadows"][0]["scope"] == "project"
    assert toolkit["shadows"][0]["value"] is False


def test_user_settings_file_is_not_reported_as_shadowing_itself(client, home):
    # the home directory is itself a known project, so its .claude/settings.json
    # IS the user settings file - it must never be flagged as a shadow
    state = client.get("/api/state")
    all_shadow_paths = [s["path"] for i in state["items"] for s in i.get("shadows", [])]
    assert str(home / ".claude" / "settings.json") not in all_shadow_paths


def test_skills_without_shadows_report_none(client):
    assert items_by_name(client.get("/api/state"), "skill")["never-used"]["shadows"] == []


def test_a_project_entry_matching_user_scope_is_not_a_shadow(client, home, tmp_path):
    """A shadow is an entry that *overrides* the user value, not one that agrees with it."""
    write_json(
        tmp_path / "proj-clean" / ".claude" / "settings.json",
        {"skillOverrides": {"stale-skill": "off"}},  # user scope already says off
    )
    stale = items_by_name(client.get("/api/state"), "skill")["stale-skill"]
    assert stale["shadows"] == []


def test_a_project_entry_differing_from_user_scope_is_a_shadow(client, tmp_path):
    write_json(
        tmp_path / "proj-clean" / ".claude" / "settings.json",
        {"skillOverrides": {"stale-skill": "name-only"}},
    )
    stale = items_by_name(client.get("/api/state"), "skill")["stale-skill"]
    assert [s["value"] for s in stale["shadows"]] == ["name-only"]


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


def test_apply_writes_skill_override_to_user_settings_and_preserves_other_keys(client, home):
    result = client.post(
        "/api/apply",
        {"changes": [{"kind": "skill", "name": "never-used", "state": "user-invocable-only"}]},
    )

    after = settings(home)
    assert after["skillOverrides"]["never-used"] == "user-invocable-only"
    assert after["skillOverrides"]["stale-skill"] == "off"  # untouched
    assert after["env"] == {"KEEP_ME": "1"}  # unmanaged key survives
    assert after["permissions"] == {"allow": ["Bash(git:*)"]}
    assert result["diff"]["skillOverrides"]["never-used"] == {
        "before": None,
        "after": "user-invocable-only",
    }


def test_apply_creates_a_timestamped_backup(client, home):
    result = client.post("/api/apply", {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]})

    backup = Path(result["backup"])
    assert backup.exists()
    assert backup.parent == home / ".claude" / "backups"
    # the backup holds the pre-apply content
    assert "never-used" not in json.loads(backup.read_text())["skillOverrides"]


def test_apply_prunes_redundant_entries_instead_of_writing_defaults(client, home):
    client.post(
        "/api/apply",
        {
            "changes": [
                {"kind": "skill", "name": "stale-skill", "state": "on"},
                {"kind": "skill", "name": "never-used", "state": "off"},
            ]
        },
    )
    # back to its default -> no entry is written for it, while the real change lands
    assert settings(home)["skillOverrides"] == {"never-used": "off"}


def test_apply_drops_the_key_entirely_when_no_overrides_remain(client, home):
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "stale-skill", "state": "on"}]})
    assert "skillOverrides" not in settings(home)


def test_apply_rejects_a_state_the_author_lock_forbids(client, home):
    code, body = client.post_expect_error(
        "/api/apply", {"changes": [{"kind": "skill", "name": "locked-skill", "state": "on"}]}
    )
    assert code == 400
    assert "locked-skill" in body["error"]
    assert "skillOverrides" not in settings(home) or "locked-skill" not in settings(home)["skillOverrides"]


def test_apply_rejects_unknown_items_and_writes_nothing(client, home):
    before = settings(home)
    code, body = client.post_expect_error(
        "/api/apply", {"changes": [{"kind": "skill", "name": "no-such-skill", "state": "off"}]}
    )
    assert code == 400
    assert "no-such-skill" in body["error"]
    assert settings(home) == before


def test_apply_rejects_an_invalid_state_and_writes_nothing(client, home):
    before = settings(home)
    code, _ = client.post_expect_error(
        "/api/apply", {"changes": [{"kind": "skill", "name": "never-used", "state": "sideways"}]}
    )
    assert code == 400
    assert settings(home) == before


def test_apply_toggles_a_plugin(client, home):
    client.post("/api/apply", {"changes": [{"kind": "plugin", "name": "toolkit@market", "enabled": False}]})
    assert settings(home)["enabledPlugins"]["toolkit@market"] is False


def test_disabling_a_plugin_removes_its_skills_from_the_inventory(client):
    client.post("/api/apply", {"changes": [{"kind": "plugin", "name": "toolkit@market", "enabled": False}]})
    skills = items_by_name(client.get("/api/state"), "skill")
    assert "toolkit:hammer" not in skills


def test_apply_toggles_an_mcp_server_into_the_disabled_list(client, home):
    client.post("/api/apply", {"changes": [{"kind": "mcp", "name": "cluster", "enabled": False}]})
    assert settings(home)["disabledMcpjsonServers"] == ["cluster"]

    client.post("/api/apply", {"changes": [{"kind": "mcp", "name": "cluster", "enabled": True}]})
    assert "disabledMcpjsonServers" not in settings(home)


def test_apply_toggles_the_bundled_master_switch(client, home):
    client.post("/api/apply", {"changes": [{"kind": "bundled-master", "name": "disableBundledSkills",
                                            "enabled": False}]})
    assert settings(home)["disableBundledSkills"] is True


def test_the_master_toggle_is_only_offered_when_extraction_failed(client, home):
    """Per spec it is the fallback path, not a permanent row."""
    assert items_by_name(client.get("/api/state"), "bundled-master") == {}

    (home / ".local" / "share" / "claude" / "versions" / "9.9.9").write_text("unrecognised build")
    assert "disableBundledSkills" in items_by_name(client.get("/api/state"), "bundled-master")


def test_disabling_an_already_off_plugin_writes_no_redundant_entry(client, home):
    """straykit is absent from enabledPlugins, so 'off' is already its default."""
    result = client.post(
        "/api/apply", {"changes": [{"kind": "plugin", "name": "straykit@market", "enabled": False}]}
    )
    assert result["diff"] == {}
    assert "straykit@market" not in settings(home)["enabledPlugins"]


def test_apply_handles_a_batch_of_mixed_changes(client, home):
    client.post(
        "/api/apply",
        {
            "changes": [
                {"kind": "skill", "name": "never-used", "state": "off"},
                {"kind": "skill", "name": "daily-driver", "state": "name-only"},
                {"kind": "plugin", "name": "offkit@market", "enabled": True},
                {"kind": "mcp", "name": "db", "enabled": False},
            ]
        },
    )
    after = settings(home)
    assert after["skillOverrides"] == {
        "stale-skill": "off",
        "never-used": "off",
        "daily-driver": "name-only",
    }
    assert after["enabledPlugins"]["offkit@market"] is True
    assert after["disabledMcpjsonServers"] == ["db"]


def test_apply_leaves_no_temp_files_behind(client, home):
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]})
    leftovers = [p.name for p in (home / ".claude").iterdir() if p.name != "settings.json" and p.is_file()]
    assert leftovers == []


def test_applying_no_changes_reports_an_empty_diff_and_makes_no_backup(client, home):
    result = client.post("/api/apply", {"changes": []})
    assert result["diff"] == {}
    assert result["backup"] is None
    assert not (home / ".claude" / "backups").exists()


def test_meter_drops_after_applying_savings(client):
    before = client.get("/api/state")["meters"]["always_paid"]
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "daily-driver", "state": "off"}]})
    assert client.get("/api/state")["meters"]["always_paid"] < before


# --------------------------------------------------------------------------- #
# undo
# --------------------------------------------------------------------------- #


def test_undo_restores_managed_keys_from_the_last_apply(client, home):
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "daily-driver", "state": "off"}]})
    client.post("/api/undo", {})
    assert "daily-driver" not in settings(home).get("skillOverrides", {})


def test_undo_keeps_unmanaged_edits_made_after_the_apply(client, home):
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "daily-driver", "state": "off"}]})

    # something else edits an unmanaged key in the meantime
    current = settings(home)
    current["env"]["ADDED_LATER"] = "yes"
    write_json(home / ".claude" / "settings.json", current)

    client.post("/api/undo", {})
    after = settings(home)
    assert after["env"]["ADDED_LATER"] == "yes"
    assert "daily-driver" not in after.get("skillOverrides", {})


def test_undo_without_a_backup_is_an_error(client):
    code, body = client.post_expect_error("/api/undo", {})
    assert code == 400
    assert "undo" in body["error"].lower()


def test_undo_ignores_backups_left_by_earlier_runs(client, home):
    """'Undo last apply' means this session's apply - not a weeks-old file on disk."""
    stale = home / ".claude" / "backups" / "context-saver-1.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({"skillOverrides": {"daily-driver": "name-only"}}))

    assert client.get("/api/state")["undo_available"] is False
    code, _ = client.post_expect_error("/api/undo", {})
    assert code == 400
    assert "daily-driver" not in settings(home).get("skillOverrides", {})


def test_undo_reverts_only_the_most_recent_apply(client, home):
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "daily-driver", "state": "off"}]})
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]})
    client.post("/api/undo", {})

    overrides = settings(home)["skillOverrides"]
    assert overrides["daily-driver"] == "off"  # first apply survives
    assert "never-used" not in overrides  # second apply reverted


def test_state_reports_whether_undo_is_available(client):
    assert client.get("/api/state")["undo_available"] is False
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]})
    assert client.get("/api/state")["undo_available"] is True


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #


def test_corrupt_user_settings_still_serves_state_but_refuses_to_apply(client, home):
    (home / ".claude" / "settings.json").write_text("{ this is not json")

    state = client.get("/api/state")
    assert state["settings_error"] is not None
    assert items_by_name(state, "skill")["daily-driver"]["state"] == "on"

    code, body = client.post_expect_error(
        "/api/apply", {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]}
    )
    assert code == 400
    assert "settings" in body["error"].lower()


def test_a_corrupt_claude_json_is_surfaced_not_swallowed(client, home):
    (home / ".claude.json").write_text("{ broken")
    state = client.get("/api/state")
    assert state["data_error"] is not None
    assert ".claude.json" in state["data_error"]
    # inventory still works, just without usage/MCP data
    assert "daily-driver" in items_by_name(state, "skill")


def test_apply_preserves_the_settings_file_mode(client, home):
    target = home / ".claude" / "settings.json"
    target.chmod(0o600)
    client.post("/api/apply", {"changes": [{"kind": "skill", "name": "never-used", "state": "off"}]})
    assert target.stat().st_mode & 0o777 == 0o600


def test_bundled_extraction_tolerates_whitespace_in_the_build(client, home):
    """Minified output is dense, but a build with pretty-printed calls must still parse."""
    (home / ".local" / "share" / "claude" / "versions" / "9.9.9").write_text(
        'var d1 = "A spaced-out bundled skill.";\n'
        'function reg(){ nu({ name: "fake-spaced", menuDescription: "m", description: d1 }) }\n'
    )
    assert "fake-spaced" in items_by_name(client.get("/api/state"), "skill")


def test_missing_settings_file_is_treated_as_empty(client, home):
    (home / ".claude" / "settings.json").unlink()
    state = client.get("/api/state")
    assert state["settings_error"] is None
    assert items_by_name(state, "skill")["stale-skill"]["state"] == "on"


def test_the_ui_page_is_served(client):
    status, content_type, body = client.get_raw("/")
    assert status == 200
    assert "text/html" in content_type
    assert "Context Saver" in body
    assert "/api/state" in body
