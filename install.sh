#!/usr/bin/env bash
# install.sh — install Baton for Claude Code.
#
#   ./install.sh             skill (~/.claude/skills/baton) + `baton` CLI (~/.local/bin/baton)
#   ./install.sh --council   only council.skill with Baton's councilor backends
#   ./install.sh --all       both
#
# The Codex quota statusline is configured per project by `baton init` (or `baton statusline install`).
# Re-running is safe: an existing non-symlink skill dir, a `baton` command that is not Baton's,
# an existing council setup and an existing council-def.sh are all left alone.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SRC="${REPO}/skills/baton"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-${HOME}/.claude/skills}"
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
  ln -sfn "${SKILL_SRC}" "${dst}"
  echo "skill   ${dst} -> ${SKILL_SRC}"
  if [[ -e "${cli}" || -L "${cli}" ]] && [[ "$(readlink "${cli}" 2>/dev/null || true)" != */skills/baton/bin/baton ]]; then
    echo "冲突：${cli} 是另一个同名程序。skill 里的 baton 命令会调用到它，请先改名或移走，再重新运行 install.sh" >&2
    STATUS=1
  else
    ln -sfn "${SKILL_SRC}/bin/baton" "${cli}"
    echo "CLI     ${cli}"
  fi
  case ":${PATH}:" in *":${BIN_DIR}:"*) ;; *) echo "提示：${BIN_DIR} 不在 PATH 中，请加入 shell 配置" ;; esac
  command -v codex >/dev/null || echo "提示：没有找到 codex CLI（npm i -g @openai/codex，然后 codex login）"
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

[[ $# -eq 0 ]] && set -- --skill
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skill) install_skill; shift ;;
    --council) install_council; shift ;;
    --all) install_skill; install_council; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
exit "${STATUS}"
