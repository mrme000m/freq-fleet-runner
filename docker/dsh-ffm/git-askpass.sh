#!/bin/sh
# git-askpass.sh — answer GitHub HTTPS credential prompts with the fleet token.
#
# git invokes `core.askPass` (this script) once per credential with the prompt
# as $1. For GitHub token auth over HTTPS the username is ignored (any value
# works) and the password is the fine-grained PAT. The token is NEVER baked or
# stored in git config — it is read from the GH_FLEET_TOKEN env var (loaded at
# container boot from the Bitwarden vault by vault_loader.sh), so token rotation
# is transparent on the next push.
#
# Wired up in entrypoint.sh: `git config --global core.askPass /app/ffm/git-askpass.sh`
case "$1" in
  Username*) printf '%s\n' "x-access-token" ;;
  Password*) printf '%s\n' "${GH_FLEET_TOKEN:-}" ;;
  *)         printf '%s\n' "${GH_FLEET_TOKEN:-}" ;;
esac