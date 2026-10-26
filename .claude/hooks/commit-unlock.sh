#!/bin/bash
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.user_prompt // empty' 2>/dev/null)

# Listen for your unlocks in the prompt chat
echo "$PROMPT" | grep -qiE '(ok commit|commit approved|you may commit)' && touch "/tmp/.cc-commit-allowed-$USER"
echo "$PROMPT" | grep -qiE '(ok push|push approved|you may push)' && touch "/tmp/.cc-push-allowed-$USER"

exit 0
