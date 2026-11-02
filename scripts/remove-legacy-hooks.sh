#!/bin/bash
# For a device that ran the pre-plugin install.sh: removes the hook entries it
# added to settings.json, so events aren't recorded twice once the plugin is on.
for f in "$HOME/.claude/settings.json" "$HOME/.claude-personal/settings.json"; do
  [ -f "$f" ] || continue
  cp "$f" "$f.bak-agentdeck-legacy"
  jq --arg cmd "$HOME/.agent-deck/hook.sh" '
    if .hooks then
      .hooks |= (map_values(map(.hooks |= map(select(.command != $cmd)))
                            | map(select(.hooks | length > 0)))
                 | with_entries(select(.value | length > 0)))
    else . end
  ' "$f.bak-agentdeck-legacy" > "$f.tmp" && mv "$f.tmp" "$f" \
    && echo "cleaned: $f (backup: $f.bak-agentdeck-legacy)" \
    || { rm -f "$f.tmp"; echo "skipped: $f (could not parse)"; }
done
