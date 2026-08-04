# Context Saver

A local click-click UI for managing Claude Code context load. Shows what every skill,
plugin and MCP server costs in always-paid context tokens, how often you actually use it,
and writes your choices to **user-scope** `~/.claude/settings.json` with a backup, a diff
preview and one-click undo.

```bash
uv run context_saver.py           # http://127.0.0.1:8787
```

No build step, no node_modules — one Python file, stdlib server, embedded page.

## Why

- `/skills` writes its toggles to **project-local** settings, so a change made in one
  directory silently doesn't apply anywhere else.
- Nothing shows you what a skill *costs*, so there's no way to prioritise trimming.
- Nothing shows you what you actually *use*.
- Hand-editing `~/.claude/settings.json` risks the hooks/permissions/env living beside it.

## What it manages

| Type | Settings key | States |
|---|---|---|
| Skills (user, plugin, bundled) | `skillOverrides` | `on` · `name-only` · `user-invocable-only` · `off` |
| Plugins | `enabledPlugins` | on / off |
| MCP servers | `disabledMcpjsonServers` | on / off |
| Bundled master switch | `disableBundledSkills` | on / off |

Those four keys are the only thing ever written. Everything else in your settings file is
copied through untouched.

`name-only` keeps the skill's name visible to the model for ~4 tokens but drops its
description. `user-invocable-only` hides it from the model entirely while `/name` still
works — usually what you want for a skill you invoke deliberately.

## The listing budget (why savings aren't always 1:1)

The CLI caps the whole skill listing at **1% of the context window** measured at 4
bytes/token — 10k tokens on a 1M window (`skillListingBudgetFraction`). Over that cap it
keeps bundled and name-only entries whole and truncates the rest to just `- name`.

So the budget meter matters more than the raw total:

- **Over the cap** — the tail is *already* truncated. Trimming buys back full descriptions
  for the skills you kept (better targeting) before it buys back tokens.
- **Under the cap** — every token you trim is a real per-turn saving.

## Meters

- **Always-paid** — user + plugin skill listing lines, the cost you pay every turn.
- **Listing budget** — total vs the 1% cap, with the overshoot.
- **Bundled (unverified)** — built-in skills read out of the CLI build. Their runtime
  gating can't be read offline, so they're subtotalled separately and never move the
  headline number. Some rows may be slash commands rather than listed skills; toggling
  them still writes a real override.
- **On-demand exposure** — MCP servers. Tool schemas load on demand, so they cost ~0 per
  turn; this is a clutter/hygiene count, deliberately **not** counted as savings.

## Guidance

Each row carries `usageCount` and `lastUsedAt` from Claude Code's own tracking, sortable
by tokens, usage or staleness. **Select never-used & stale** pre-stages everything unused
or older than 60 days as `user-invocable-only` — it only stages, never applies.

## Safety

- Writes go to user scope only, so choices apply in every project.
- Clicks stage; **Review & apply** shows the JSON diff of the managed keys and writes only
  after you confirm it.
- Timestamped backup to `~/.claude/backups/` before every write; atomic fsync+rename that
  preserves the file's mode.
- **Undo last apply** restores the managed keys only, so unrelated edits made in the
  meantime survive. It covers this session's applies, not stale backups from earlier runs.
- POST requests are refused from cross-site origins and non-JSON content types — any web
  page can reach a localhost server, and this one writes your settings.
- Project/local settings that shadow a user-level value are flagged on the row, with the
  shadowing file's path.
- Changes take effect in **new** sessions.

## Notes

- First run scans the ~270MB CLI build for bundled skills (~13s) and caches the result in
  `~/.claude/context-saver/bundled.json`, keyed by the build's mtime+size. Later loads are
  instant; a CLI upgrade re-scans.
- A plugin installed but absent from `enabledPlugins` is treated as **off**, matching what
  the CLI actually lists.
- `--home` (or `CONTEXT_SAVER_HOME`) points the tool at a different root; that's the seam
  the tests drive.
- `--context-window` sets the window the 1% budget is computed from (default 1,000,000);
  `/api/state?context_window=200000` does the same per request.

## Tests

```bash
python3 -m pytest -q      # 43 tests through the HTTP API against a fake home
python3 -m ruff check .
```
