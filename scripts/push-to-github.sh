#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

backup_label_raw="${BACKUP_LABEL:-${1:-}}"
backup_label_trimmed="$(printf '%s' "$backup_label_raw" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
backup_label_slug="$(
  printf '%s' "$backup_label_trimmed" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'
)"

if [[ "${SKIP_GITHUB_PUSH:-}" == "1" || "${SKIP_GITHUB_PUSH:-}" == "true" ]]; then
  echo "[push-to-github] SKIP_GITHUB_PUSH is set; skipping push."
  exit 0
fi

if ! command -v git >/dev/null 2>&1; then
  echo "[push-to-github] git is not installed; skipping push."
  exit 0
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[push-to-github] not a git repository; skipping push."
  exit 0
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "[push-to-github] no 'origin' remote configured; skipping push."
  exit 0
fi

if ! git config user.name >/dev/null 2>&1 || ! git config user.email >/dev/null 2>&1; then
  echo "[push-to-github] git user.name or user.email is not configured; skipping push."
  exit 0
fi

current_branch="$(git branch --show-current)"
if [[ -z "$current_branch" ]]; then
  echo "[push-to-github] detached HEAD; cannot push automatically."
  exit 0
fi

upstream_ref=""
if upstream_ref="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)"; then
  read -r behind_count ahead_count < <(git rev-list --left-right --count "${upstream_ref}...HEAD")
  if [[ "$behind_count" != "0" ]]; then
    echo "[push-to-github] local branch is behind ${upstream_ref}; skipping push to avoid a non-fast-forward push."
    exit 0
  fi
fi

if [[ -n "$(git status --porcelain)" ]]; then
  git add -A

  if ! git diff --cached --quiet; then
    stamp_human="$(date '+%Y-%m-%d %H:%M:%S %z')"
    stamp_tag="$(date '+%Y%m%d-%H%M%S')"
    commit_message="chore: automated backup [${stamp_human}]"
    if [[ -n "$backup_label_trimmed" ]]; then
      commit_message="${commit_message} [${backup_label_trimmed}]"
    fi

    backup_tag="backup-${stamp_tag}"
    if [[ -n "$backup_label_slug" ]]; then
      backup_tag="backup-${backup_label_slug}-${stamp_tag}"
    fi

    if git rev-parse -q --verify "refs/tags/${backup_tag}" >/dev/null 2>&1; then
      backup_tag="${backup_tag}-$(git rev-parse --short HEAD)"
    fi

    git commit -m "$commit_message"
    git tag "$backup_tag"

    git push --atomic origin "HEAD:${current_branch}" "$backup_tag"
    echo "[push-to-github] pushed to origin/${current_branch} with tag ${backup_tag}."
  else
    echo "[push-to-github] no staged changes after add; skipping commit/push."
  fi
else
  echo "[push-to-github] no changes detected; skipping push."
fi
