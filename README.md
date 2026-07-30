# shellbox

Agent shell sessions with browser rendering on Databricks.

Two halves:

- **`shellbox-mcp`** — a stdio MCP server that ships onto an agent host (a Databricks Sandbox / "Lakebox"). Gives the agent persistent, named, tmux-backed shell sessions and registers them centrally.
- **`shellbox-app`** — a Databricks App that renders those sessions as live, interactive browser terminals.

Sessions are created by agents, attached to by humans, and reaped on inactivity. Harness-agnostic (Claude Code, Codex, Buzz — anything speaking MCP).

Work in progress. Design and phased plan: [epic #9](https://github.com/IceRhymers/shellbox/issues/9).
