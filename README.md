# Agent Deck

A local dashboard for every Claude Code agent on this Mac, across accounts.
It shows which sessions need your input, which have output ready, which
subagents each main agent is running, and jumps to the right terminal (iTerm2,
Android Studio, IntelliJ...). macOS only.

## Install

Plugins are installed per account, so run this once for each account. In a
Claude session, or from a terminal with `claude plugin ...`:

```
/plugin marketplace add activat0r/agent-deck    # or a local path to this folder
/plugin install agent-deck@agent-deck
```

For a second account, run the same two commands with its config dir, e.g.
`CLAUDE_CONFIG_DIR=~/.claude-personal claude plugin marketplace add ...`.

The next session that starts sets everything up: it copies the server to
`~/.agent-deck`, adds the `com.agentdeck.server` LaunchAgent, starts it, and
opens http://localhost:7777. `/agent-deck:open` opens the dashboard later.

Needs `jq` (built into macOS 15+, or `brew install jq`) and `python3`.

## Accounts

The default `~/.claude` is labelled **Work** and `~/.claude-personal`
**Personal** (`ACCOUNTS` in `app/server.py`). Any other `CLAUDE_CONFIG_DIR` is
shown under its folder name.

## Updating

Bump `version` in `.claude-plugin/plugin.json`, then `/plugin update`. The next
session start copies the newer server into `~/.agent-deck` and restarts it. An
older version installed in another account never replaces a newer one.

## Moving from the old install.sh setup

That setup added the hook to both `settings.json` files directly. Remove those
entries before installing the plugin, or every event is recorded twice:

```
bash scripts/remove-legacy-hooks.sh
```

## Uninstall

```
/plugin uninstall agent-deck@agent-deck      # in each account
bash scripts/uninstall.sh                    # stop the server; --purge also deletes ~/.agent-deck
```
