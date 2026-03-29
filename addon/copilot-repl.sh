#!/bin/sh
# copilot-repl.sh -- persistent interactive wrapper for GitHub Copilot CLI
# Keeps the PTY session alive between queries.

clear

printf "\033[1;32m  GitHub Copilot CLI\033[0m  \033[2mHome Assistant\033[0m\n"
printf "\033[90m  -----------------------------------------------\033[0m\n"
printf "  \033[33mType a question\033[0m      suggest a shell command\n"
printf "  \033[33mexplain <cmd>\033[0m        explain what a command does\n"
printf "  \033[33mhelp\033[0m                 show this message\n"
printf "  \033[33mexit\033[0m                 close session\n"
printf "\033[90m  -----------------------------------------------\033[0m\n\n"

# Verify the copilot binary is accessible
if ! command -v copilot >/dev/null 2>&1; then
    printf "\033[31m  [error] copilot not found in PATH.\033[0m\n"
    printf "  Check add-on logs. Waiting 10s then retrying...\n\n"
    sleep 10
    exec "$0"
fi

while true; do
    printf "\033[1;36mcopilot\033[0m \033[90m>\033[0m "

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
            printf "\n  \033[33mType a question\033[0m      suggest a shell command\n"
            printf "  \033[33mexplain <cmd>\033[0m        explain what a command does\n"
            printf "  \033[33mexit\033[0m                 close session\n\n"
            ;;
        explain\ *)
            printf "\n"
            copilot explain "${input#explain }" 2>&1
            ;;
        suggest\ *)
            printf "\n"
            copilot suggest -s sh "${input#suggest }" 2>&1
            ;;
        *)
            printf "\n"
            copilot suggest -s sh "$input" 2>&1
            ;;
    esac

    printf "\n"
done