#!/bin/bash
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

[ -z "$COMMAND" ] && exit 0

# Check for unauthorized git commit
if echo "$COMMAND" | grep -qE '^\s*git\s+commit'; then
    MARKER="/tmp/.cc-commit-allowed-$USER"
    if [ ! -f "$MARKER" ]; then
        echo "BLOCKED: git commit requires explicit user permission." >&2
        echo "Say 'ok commit' or 'commit approved' to unlock." >&2
        exit 2
    fi
    rm -f "$MARKER"  # Consume marker for one-time use
fi

# Check for unauthorized git push
if echo "$COMMAND" | grep -qE '^\s*git\s+push'; then
    MARKER="/tmp/.cc-push-allowed-$USER"
    if [ ! -f "$MARKER" ]; then
        echo "BLOCKED: git push requires explicit user permission." >&2
        echo "Say 'ok push' or 'push approved' to unlock." >&2
        exit 2
    fi
    rm -f "$MARKER"  # Consume marker for one-time use
fi

exit 0