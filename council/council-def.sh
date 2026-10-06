# council-def.sh — councilor backends for council.skill, as used by Baton.
#
# Library file, sourced by council.sh (https://github.com/ParadoxZW/council.skill).
# install.sh copies it to ${COUNCILOR_LIB:-~/.local/bin/council-def.sh} when that file does not
# exist yet. To merge into an existing council-def.sh, append this whole file: every function
# carries its own defaults and the variables are Baton-prefixed, so nothing you already set
# is overwritten, and the functions also work under council.sh's `set -u`.
#
# Needs no API keys: the Claude backends use the logged-in Claude Code subscription, the Codex
# backend uses the logged-in Codex CLI (~/.codex).
#
# council-cc-opus / council-cc-sonnet and the env-unset list are adapted from council.skill's
# council-def.example.sh — Copyright (c) 2026 ParadoxZW, MIT License.

# Official Claude: clear inherited third-party overrides so "opus" always means the official model.
council-cc-opus() {
  env -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL \
    -u ANTHROPIC_DEFAULT_OPUS_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL -u ANTHROPIC_DEFAULT_HAIKU_MODEL \
    -u CLAUDE_CODE_SUBAGENT_MODEL \
    claude --model "${BATON_COUNCIL_OPUS_MODEL:-opus}" --effort max "$@"
}
council-cc-sonnet() {
  env -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL \
    -u ANTHROPIC_DEFAULT_OPUS_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL -u ANTHROPIC_DEFAULT_HAIKU_MODEL \
    -u CLAUDE_CODE_SUBAGENT_MODEL \
    claude --model "${BATON_COUNCIL_SONNET_MODEL:-sonnet}" --effort max "$@"
}

# Codex (GPT-6.1-Sol): read-only sandbox, same reasoning depth as the Baton executor, fast mode off.
# The query arrives on stdin. Uses Codex quota.
council-codex-sol() {
  codex exec \
    -m "${BATON_COUNCIL_CODEX_MODEL:-gpt-6.1-sol}" \
    -c "model_reasoning_effort=\"${BATON_COUNCIL_CODEX_EFFORT:-xhigh}\"" \
    -c 'service_tier="default"' \
    --disable fast_mode \
    --sandbox read-only \
    --skip-git-repo-check \
    -
}
