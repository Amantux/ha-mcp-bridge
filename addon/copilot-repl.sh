#!/bin/sh
# copilot-repl.sh — persistent interactive wrapper for the GitHub Copilot CLI
# Keeps the PTY session alive; routes user input to `copilot suggest` / `copilot explain`

clear
printf "\033[1;32m"
printf "  ██████╗ ██████╗ ██████╗ ██╗██╗      ██████╗ ████████╗\n"
printf " ██╔════╝██╔═══██╗██╔══██╗██║██║     ██╔═══██╗╚══██╔══╝\n"
printf " ██║     ██║   ██║██████╔╝██║██║     ██║   ██║   ██║   \n"
printf " ██║     ██║   ██║██╔═══╝ ██║██║     ██║   ██║   ██║   \n"
printf " ╚██████╗╚██████╔╝██║     ██║███████╗╚██████╔╝   ██║   \n"
printf "  ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝ ╚═════╝    ╚═╝   \n"
printf "\033[0m"
printf "\n\033[1;37m  GitHub Copilot CLI  \033[2m— Home Assistant\033[0m\n\n"
printf "\033[2m  Usage:\033[0m\n"
printf "  \033[33mType any question\033[0m    → suggest a shell command\n"
printf "  \033[33mexplain <command>\033[0m    → explain what a command does\n"
printf "  \033[33mexit\033[0m                 → close this session\n"
printf "\n\033[90m─────────────────────────────────────────────────────\033[0m\n\n"

# Verify the copilot binary is accessible
if ! command -v copilot >/dev/null 2>&1; then
    printf "\033[31m[error] copilot binary not found in PATH\033[0m\n"
    printf "Check add-on logs. Waiting 10s and retrying…\n"
    sleep 10
    exec "$0"
fi

while true; do
    # Show prompt
    printf "\033[1;36mcopilot\033[0m \033[90m›\033[0m "

    # Read input (IFS= preserves leading/trailing spaces; -r no backslash interpret)
    IFS= read -r input

    # EOF (Ctrl-D) → exit
    [ $? -ne 0 ] && printf "\n\033[90m[bye]\033[0m\n" && break

    # Trim leading/trailing whitespace
    input="$(printf '%s' "$input" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

    # Skip empty lines
    [ -z "$input" ] && continue

    # Built-in commands
    case "$input" in
        exit|quit|bye)
            printf "\033[90m[session closed]\033[0m\n"
            exit 0
            ;;
        explain\ *)
            query="${input#explain }"
            printf "\n"
            copilot explain "$query"
            ;;
        suggest\ *)
            query="${input#suggest }"
            printf "\n"
            copilot suggest -s sh "$query"
            ;;
        help|'?')
            printf "\n  \033[33mType any question\033[0m    → suggest a shell command\n"
            printf "  \033[33mexplain <command>\033[0m    → explain what a command does\n"
            printf "  \033[33mexit\033[0m                 → close this session\n\n"
            ;;
        *)
            # Default: treat as a question for copilot suggest
            printf "\n"
            copilot suggest -s sh "$input"
            ;;
    esac

    printf "\n"
done
