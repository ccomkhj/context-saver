# Context Saver

A local click-click UI for cutting Claude Code's context load. See what every skill, plugin
and MCP server costs in **always-paid context tokens**, and turn off what you don't use.

![Context Saver dashboard](docs/dashboard.png)

<sub>Screenshot uses a synthetic demo home, not real inventory.</sub>

## Why this matters

Every installed skill puts a `- name: description` line into a listing injected into
**every** turn, used or not. That's a standing tax, and context engineering says it's the
wrong one to pay:

- **The window is a finite attention budget.** Descriptions for capabilities you never
  invoke crowd out the task at hand.
- **More context is not better context.** Quality degrades as irrelevant material
  accumulates — you want the smallest high-signal set, not the largest set of options.
- **Irrelevant options are distractors.** 120 skill descriptions make a
  plausible-but-wrong pick likelier, so trimming improves *targeting*, not just cost.
- **Pay on demand, not up front.** A skill's listing line is always paid; its body loads
  only when invoked, and MCP tool schemas are deferred too. Knowing which costs are
  always-paid is what tells you where trimming actually helps.

## What it manages

| Type | Settings key | States |
|---|---|---|
| Skills (user, plugin, bundled) | `skillOverrides` | `on` · `name-only` · `user-invocable-only` · `off` |
| Plugins | `enabledPlugins` | on / off |
| MCP servers | `disabledMcpjsonServers` | on / off |
| Bundled master switch | `disableBundledSkills` | on / off |

## How to run

```bash
uv run context_saver.py           # http://127.0.0.1:8787
```

One Python file, stdlib server, embedded page — no build step and no network. Clicks stage;
**Review & apply** shows the JSON diff and writes user-scope `~/.claude/settings.json` after
you confirm, with a timestamped backup and one-click undo. Changes take effect in new
sessions.
