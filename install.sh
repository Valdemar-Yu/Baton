#!/usr/bin/env bash
# install.sh — install Baton v3 for Antigravity CLI (`agy`) and Claude Code.
#
#   ./install.sh             skill (~/.gemini/config/skills/baton + ~/.claude/skills/baton) + `baton` CLI (~/.local/bin/baton) + agy statusline
#   ./install.sh --agy       only Antigravity CLI skill (~/.gemini/config/skills/baton) + agy statusline (~/.local/share/agy-statusline/statusline.py)
#   ./install.sh --council   only council.skill with Baton's councilor backends
#   ./install.sh --council-codex  only the Codex council skill
#   ./install.sh --all       Baton (agy + Claude) + Claude council + Codex council
#
# Re-running is safe: an existing non-symlink skill dir, a `baton` command that is not Baton's,
# an existing council setup and an existing council-def.sh are all left alone.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SRC="${REPO}/skills/baton"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-${HOME}/.claude/skills}"
AGY_SKILLS_DIR="${AGY_SKILLS_DIR:-${HOME}/.gemini/config/skills}"
AGY_STATUSLINE_DIR="${AGY_STATUSLINE_DIR:-${HOME}/.local/share/agy-statusline}"
BIN_DIR="${BATON_BIN_DIR:-${HOME}/.local/bin}"
# council.sh reads its backends from here (same default and override as council.skill itself)
COUNCIL_DEF="${COUNCILOR_LIB:-${HOME}/.local/bin/council-def.sh}"
STATUS=0
BATON_CLONE_TMP=""
trap '[[ -z "${BATON_CLONE_TMP:-}" ]] || rm -rf "${BATON_CLONE_TMP}"' EXIT

install_skill() {
  mkdir -p "${SKILLS_DIR}" "${BIN_DIR}"
  chmod +x "${SKILL_SRC}/bin/baton" "${SKILL_SRC}"/scripts/*.py
  local dst="${SKILLS_DIR}/baton" cli="${BIN_DIR}/baton"
  if [[ -e "${dst}" && ! -L "${dst}" ]]; then
    echo "跳过：${dst} 已存在且不是软链接（可能是另一份 Baton），请先移走再安装；CLI 也没有改动" >&2
    STATUS=1
    return
  fi
  if [[ -L "${dst}" ]]; then
    local old_skill
    old_skill=$(readlink "${dst}" 2>/dev/null || true)
    if [[ "${old_skill}" != "${SKILL_SRC}" ]]; then
      echo "更新软链接：${dst}（${old_skill} -> ${SKILL_SRC}）"
    fi
  else
    echo "skill   ${dst} -> ${SKILL_SRC}"
  fi
  ln -sfn "${SKILL_SRC}" "${dst}"
  if [[ -L "${cli}" ]]; then
    local old_cli
    old_cli=$(readlink "${cli}" 2>/dev/null || true)
    if [[ "${old_cli}" != "${SKILL_SRC}/bin/baton" ]]; then
      echo "更新软链接：${cli}（${old_cli} -> ${SKILL_SRC}/bin/baton）"
    fi
    ln -sfn "${SKILL_SRC}/bin/baton" "${cli}"
    echo "CLI     ${cli} -> ${SKILL_SRC}/bin/baton"
  elif [[ -e "${cli}" ]]; then
    echo "冲突：${cli} 是另一个同名程序。skill 里的 baton 命令会调用到它，请先改名或移走，再重新运行 install.sh" >&2
    STATUS=1
  else
    ln -sfn "${SKILL_SRC}/bin/baton" "${cli}"
    echo "CLI     ${cli} -> ${SKILL_SRC}/bin/baton"
  fi
  case ":${PATH}:" in *":${BIN_DIR}:"*) ;; *) echo "提示：${BIN_DIR} 不在 PATH 中，请加入 shell 配置" ;; esac
  if command -v agy >/dev/null; then
    echo "agy     已找到：$(command -v agy)"
  else
    echo "提示：没有找到 agy CLI（Antigravity CLI，v3 Conductor/Advisor/Monitor 核心依赖）"
  fi
  if command -v claude >/dev/null; then
    echo "Claude  已找到：$(command -v claude)"
  else
    echo "提示：没有找到 claude CLI（v3 默认 Claude Opus XHigh Executor）"
  fi
  command -v codex >/dev/null || echo "提示：没有找到 codex CLI（仅 v2-codex 预设需要）"
  if command -v ollama >/dev/null; then
    echo "Ollama  已找到：$(command -v ollama)"
  else
    echo "提示：没有找到 Ollama（可选，用于 mem0 本地语义记忆 qwen3-embedding:0.6b）"
  fi
  if command -v hermes >/dev/null; then
    echo "Hermes  已找到：$(command -v hermes)"
  else
    echo "提示：没有找到 hermes（仅 v2 Hermes 预设需要）"
  fi
  install_agy
  echo "下一步（不会自动执行）：baton init；可选 baton memory install"
}

install_agy() {
  mkdir -p "${AGY_SKILLS_DIR}" "${AGY_STATUSLINE_DIR}"
  chmod +x "${SKILL_SRC}/scripts/agy_statusline.py"
  local agy_dst="${AGY_SKILLS_DIR}/baton"
  if [[ -e "${agy_dst}" && ! -L "${agy_dst}" ]]; then
    echo "跳过：${agy_dst} 已存在且不是软链接，请先移走再安装" >&2
    STATUS=1
  else
    ln -sfn "${SKILL_SRC}" "${agy_dst}"
    echo "agy     ${agy_dst} -> ${SKILL_SRC}"
  fi
  local sl_dst="${AGY_STATUSLINE_DIR}/statusline.py"
  cp "${SKILL_SRC}/scripts/agy_statusline.py" "${sl_dst}"
  chmod +x "${sl_dst}"
  echo "status  ${sl_dst}（在 agy 中运行 /statusline python3 ${sl_dst} 即可启用）"
}

install_council() {
  local dst="${SKILLS_DIR}/council"
  if [[ -d "${dst}" ]]; then
    echo "council 已安装：${dst}（保留原配置；要用 Baton 的顾问组合，参考 ${REPO}/council/config.json）"
  else
    mkdir -p "${SKILLS_DIR}"
    BATON_CLONE_TMP=$(mktemp -d)
    git clone --depth 1 -q https://github.com/ParadoxZW/council.skill "${BATON_CLONE_TMP}/council.skill"
    cp -R "${BATON_CLONE_TMP}/council.skill/skills/council" "${dst}"
    cp "${dst}/config.json" "${dst}/config.json.orig"
    cp "${REPO}/council/config.json" "${dst}/config.json"
    rm -rf "${BATON_CLONE_TMP}"
    BATON_CLONE_TMP=""
    echo "council ${dst}（顾问：council-cc-opus + council-codex-sol；原始配置在 config.json.orig）"
  fi
  if [[ -e "${COUNCIL_DEF}" ]]; then
    echo "保留已有 ${COUNCIL_DEF}"
  else
    mkdir -p "$(dirname "${COUNCIL_DEF}")"
    cp "${REPO}/council/council-def.sh" "${COUNCIL_DEF}"
    echo "backend ${COUNCIL_DEF}"
  fi
  # every councilor named in the active config must be a function in council-def.sh
  local names missing
  names=$(python3 -c '
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    c = {}
c = c if isinstance(c, dict) else {}
n = [x for x in (c.get("councilors") or []) if isinstance(x, str)] or ["council-cc-glm", "council-codex-gpt", "council-cc-kimi"]
if c.get("chief_backend"):
    n.append(c["chief_backend"])
print(" ".join(n))' "${dst}/config.json" 2>/dev/null || echo "")
  # shellcheck disable=SC2086
  missing=$(bash -c 'source "$1" >/dev/null 2>&1; shift; for f in "$@"; do type "$f" >/dev/null 2>&1 || echo "$f"; done' _ "${COUNCIL_DEF}" ${names})
  if [[ -n "${missing}" ]]; then
    echo "注意：${COUNCIL_DEF} 里没有定义这些顾问：$(echo ${missing})" >&2
    echo "      把 ${REPO}/council/council-def.sh 整个文件追加到 ${COUNCIL_DEF} 末尾即可（函数自带默认值，不会覆盖你已有的设置）" >&2
    STATUS=1
  fi
}

install_council_codex() {
  local codex_home="${CODEX_HOME:-${HOME}/.codex}"
  local dst="${codex_home}/skills/council"
  if [[ -e "${dst}" || -L "${dst}" ]]; then
    echo "council (Codex) 已安装：${dst}（保留原配置）"
  else
    mkdir -p "${codex_home}/skills"
    cp -R "${REPO}/council/codex" "${dst}"
    echo "council (Codex) ${dst}（顾问：council-cc-opus + council-codex-sol）"
  fi
  if [[ -e "${COUNCIL_DEF}" ]]; then
    echo "保留已有 ${COUNCIL_DEF}"
  else
    mkdir -p "$(dirname "${COUNCIL_DEF}")"
    cp "${REPO}/council/council-def.sh" "${COUNCIL_DEF}"
    echo "backend ${COUNCIL_DEF}"
  fi
  # Keep the same councilor-function validation as the Claude installation.
  local names missing
  names=$(python3 -c '
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    c = {}
c = c if isinstance(c, dict) else {}
n = [x for x in (c.get("councilors") or []) if isinstance(x, str)] or ["council-cc-opus", "council-codex-sol"]
if c.get("chief_backend"):
    n.append(c["chief_backend"])
print(" ".join(n))' "${dst}/config.json" 2>/dev/null || echo "")
  # shellcheck disable=SC2086
  missing=$(bash -c 'source "$1" >/dev/null 2>&1; shift; for f in "$@"; do type "$f" >/dev/null 2>&1 || echo "$f"; done' _ "${COUNCIL_DEF}" ${names})
  if [[ -n "${missing}" ]]; then
    echo "注意：${COUNCIL_DEF} 里没有定义这些顾问：$(echo ${missing})" >&2
    echo "      把 ${REPO}/council/council-def.sh 整个文件追加到 ${COUNCIL_DEF} 末尾即可（函数自带默认值，不会覆盖你已有的设置）" >&2
    STATUS=1
  fi
}

[[ $# -eq 0 ]] && set -- --skill
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skill) install_skill; shift ;;
    --agy) install_agy; shift ;;
    --council) install_council; shift ;;
    --council-codex) install_council_codex; shift ;;
    --all) install_skill; install_council; install_council_codex; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
exit "${STATUS}"
