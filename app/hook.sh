#!/bin/bash
# Agent Deck hook: queues the Claude Code hook event for the dashboard server.
# Must never block or fail the session, so every error is swallowed.
dir="$HOME/.agent-deck"
plugin="$(cd "$(dirname "$0")" && pwd)"
label=com.agentdeck.server
plist="$HOME/Library/LaunchAgents/$label.plist"
jq=$(command -v jq || echo /usr/bin/jq)
mkdir -p "$dir"
input=$(cat)

# Project and host-app context is only gathered on the events that can change
# it, so per-tool events stay cheap.
app="" root="" ide_root="" branch=""
case $input in
*'"SessionStart"'* | *'"UserPromptSubmit"'*)
  # The first .app ancestor is the terminal host: iTerm2, Android Studio, IntelliJ...
  pid=$PPID
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    line=$(ps -o ppid=,comm= -p "$pid" 2>/dev/null) || break
    comm=${line#*[0-9] }
    if [[ $comm == *.app/Contents/* ]]; then app=${comm%%.app/Contents/*}.app; break; fi
    pid=$(echo "$line" | awk '{print $1}')
    [ "${pid:-1}" -le 1 ] && break
  done
  cwd=$(printf '%s' "$input" | "$jq" -r '.cwd // empty' 2>/dev/null)
  if [ -n "$cwd" ]; then
    root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)
    # A home folder kept in git isn't a project, so ignore it.
    if [ -n "$root" ] && [ "$root" != "$HOME" ]; then
      branch=$(git -C "$cwd" branch --show-current 2>/dev/null)
    else
      root=""
    fi
    # JetBrains opens projects by their .idea folder's parent.
    d=$cwd
    while [ -n "$d" ] && [ "$d" != "/" ]; do
      [ -d "$d/.idea" ] && { ide_root=$d; break; }
      d=$(dirname "$d")
    done
  fi
  ;;
esac

printf '%s' "$input" | "$jq" -c \
  --arg iterm "${ITERM_SESSION_ID:-}" \
  --arg term "${TERM_PROGRAM:-${TERMINAL_EMULATOR:-}}" \
  --arg cfg "${CLAUDE_CONFIG_DIR:-}" \
  --arg app "$app" --arg root "$root" --arg ide "$ide_root" --arg branch "$branch" \
  --argjson ts "$(date +%s)" '
  del(.tool_response)
  | if (.tool_input | type) == "object" then
      .tool_input |= with_entries(.value |= (if type == "string" then .[0:400] else . end))
    else . end
  | if (.last_assistant_message | type) == "string" then
      .last_assistant_message |= .[0:2000]
    else . end
  | . + {_iterm: $iterm, _term: $term, _config: $cfg, _ts: $ts,
         _app: $app, _root: $root, _ide_root: $ide, _branch: $branch}
' >> "$dir/events.jsonl" 2>/dev/null

[[ $input == *'"SessionStart"'* ]] || exit 0

# Install or upgrade the server. The plugin lives in a per-account, per-version
# cache folder, so the server runs from a stable copy in ~/.agent-deck that the
# LaunchAgent points at. Each account installs the plugin separately; only a
# newer version replaces the copy, so two accounts on different versions don't
# keep swapping it back and forth.
new=$("$jq" -r .version "$plugin/../.claude-plugin/plugin.json" 2>/dev/null)
old=$(cat "$dir/.version" 2>/dev/null)
restart=""
if [ -n "$new" ] && [ "$new" != "$old" ] &&
  [ "$(printf '%s\n%s\n' "${old:-0}" "$new" | sort -V | tail -1)" = "$new" ]; then
  cp "$plugin/server.py" "$plugin/index.html" "$dir/" && echo "$new" > "$dir/.version" && restart=1
fi

if [ ! -f "$plist" ]; then
  py=""
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3)" /usr/bin/python3; do
    [ -x "$c" ] && { py=$c; break; }
  done
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$py</string><string>$dir/server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$dir/server.log</string>
</dict></plist>
EOF
  /bin/launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1
fi

# Make sure the single per-device server is up; it opens the dashboard itself
# when no tab is showing it. A second server can't start because the port is
# already bound.
if [ -n "$restart" ]; then
  /bin/launchctl kickstart -k "gui/$(id -u)/$label" >/dev/null 2>&1
elif ! /usr/bin/curl -s -m 1 -o /dev/null http://127.0.0.1:7777/; then
  /bin/launchctl kickstart "gui/$(id -u)/$label" >/dev/null 2>&1 \
    || /bin/launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1
fi
exit 0
