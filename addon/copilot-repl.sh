#!/bin/sh
# copilot-repl.sh -- GitHub Copilot chat terminal (calls the /chat REST API)
# Runs inside the PTY session started by ha-mcp-bridge.

CHAT_URL="http://localhost:8099/chat"

clear
printf "\033[1;32m  GitHub Copilot\033[0m  \033[2mHome Assistant\033[0m\n"
printf "\033[90m  -----------------------------------------------\033[0m\n"
printf "  \033[33mType anything\033[0m  to chat with GitHub Copilot\n"
printf "  \033[33mexit\033[0m / \033[33mquit\033[0m    close this session\n"
printf "\033[90m  -----------------------------------------------\033[0m\n\n"

while true; do
    printf "\033[1;36mYou\033[0m \033[90m>\033[0m "

    IFS= read -r input
    [ $? -ne 0 ] && printf "\n\033[90m  [bye]\033[0m\n" && exit 0

    # Trim whitespace
    input="$(printf '%s' "$input" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$input" ] && continue

    case "$input" in
        exit|quit|bye)
            printf "\033[90m  [session closed]\033[0m\n"
            exit 0
            ;;
        help|'?')
            printf "\n  \033[33mType any question\033[0m to chat with GitHub Copilot\n"
            printf "  \033[33mexit\033[0m to close session\n\n"
            continue
            ;;
    esac

    printf "\n\033[90m  Thinking\342\200\246\033[0m\n\n"

    # Call the /chat API via Python3 (always available); pass prompt via env
    # to avoid all shell-escaping issues with quotes, newlines, etc.
    response=$(COPILOT_PROMPT="$input" CHAT_URL="$CHAT_URL" python3 - <<'PYEOF'
import os, urllib.request, json, sys

prompt   = os.environ.get("COPILOT_PROMPT", "")
chat_url = os.environ.get("CHAT_URL", "http://localhost:8099/chat")

req = urllib.request.Request(
    chat_url,
    data=json.dumps({"prompt": prompt}).encode(),
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read())
    txt = d.get("output") or d.get("error") or "(no response)"
    print(txt)
except Exception as e:
    print(f"Error: {e}")
PYEOF
)

    printf "\033[1;32mCopilot\033[0m \033[90m>\033[0m\n%s\n" "$response"
    printf "\n"
done