#!/bin/bash
# Stops the Agent Deck server and removes its LaunchAgent. Pass --purge to also
# delete ~/.agent-deck (events, dismissals, the server copy).
launchctl bootout "gui/$(id -u)/com.agentdeck.server" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.agentdeck.server.plist"
[ "$1" = "--purge" ] && rm -rf "$HOME/.agent-deck"
echo "Agent Deck server removed. Uninstall the plugin in each account with: /plugin uninstall agent-deck@agent-deck"
