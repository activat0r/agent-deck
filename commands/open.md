---
description: Open the Agent Deck dashboard in the browser
allowed-tools: Bash(open http://localhost:7777), Bash(curl -s -m 2 -o /dev/null -w %{http_code} http://127.0.0.1:7777/)
---
Run `curl -s -m 2 -o /dev/null -w %{http_code} http://127.0.0.1:7777/`. If it prints 200, run `open http://localhost:7777` and reply with one line saying the deck is open. Otherwise say the server isn't running yet: it starts on the next Claude session start, or the user can run `launchctl kickstart gui/$(id -u)/com.agentdeck.server`.
