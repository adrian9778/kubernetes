#!/usr/bin/env bash
# fork 上游同步脚本
#
# 做两件事：
#   1) 把 fork 的主分支严格对齐上游（reset --hard，不留 fork 独有提交）
#   2) 把工作分支对齐到最新主分支，逐个保留其独有提交
#
# SYNC_MODE 两种取值：
#   merge  (默认) git merge main  → 原提交 SHA 不变、不重写历史、普通推送；代价是多一个 merge commit
#   rebase        git rebase main → 历史线性；但每个独有提交 SHA 被重写、需 --force-with-lease
# 两种模式都不丢工作分支的独有提交。绝不用 squash（会把独有提交压没，等于丢提交）。
#
# 用法：
#   ./sync.sh --dry-run                      # 先看将做什么，不改任何状态
#   ./sync.sh                                # 默认 merge
#   SYNC_MODE=rebase ./sync.sh               # 切 rebase
#   UPSTREAM_URL=git@host:o/r.git ./sync.sh  # 临时换上游
#
# 安全边界：上游只读、从不 push；所有写操作只落自己的 fork；主分支严格等于上游；
#           工作分支的处理只落在 WORK_BRANCH 上；本地未提交的 WIP 全程只 stash、对齐完弹回，不 commit 不 push。

set -euo pipefail

# ── 可配置项（按项目改这里）─────────────────────────────────────
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_URL="${UPSTREAM_URL:-git@github.adrian9778:kubernetes/kubernetes.git}"
MAIN_REMOTE="${MAIN_REMOTE:-origin}"
MAIN_BRANCH="${MAIN_BRANCH:-master}"
WORK_BRANCH="${WORK_BRANCH:-adrian}"
SYNC_MODE="${SYNC_MODE:-merge}"
# ──────────────────────────────────────────────────────────────

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log()  { printf '\033[1;34m[sync]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[sync]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[sync]\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if (( DRY_RUN )); then log "(dry-run) $*"; else log "$*"; "$@"; fi
}

cd_to_git_root() {
  local toplevel
  toplevel="$(git rev-parse --show-toplevel 2>/dev/null)" || die "不在 git 仓库里"
  cd "$toplevel"
}

STASHED=0
STASH_SHA=""

stash_workdir() {
  if [[ -z "$(git status --porcelain)" ]]; then STASHED=0; return; fi
  if (( DRY_RUN )); then log "(dry-run) git stash push --include-untracked ..."; STASHED=0; return; fi
  local out
  out="$(git stash push --include-untracked -m "sync.sh auto-stash $(date '+%F %T')")" || die "stash 失败"
  log "$out"
  STASHED=1
  STASH_SHA="$(git rev-parse 'stash@{0}' 2>/dev/null || true)"
}

restore_one_stash() {
  if (( ! STASHED )); then return; fi
  if [[ -z "$STASH_SHA" ]]; then warn "未记录到本次 stash 的 SHA, 跳过自动还原, 请手动 git stash list 检查"; return; fi
  local match
  match="$(git stash list --format='%H %gd' | awk -v s="$STASH_SHA" '$1==s{print $2; exit}')"
  if [[ -z "$match" ]]; then warn "本次 stash 已不在栈中, 跳过还原"; STASHED=0; return; fi
  if git stash apply --index "$match"; then
    git stash drop "$match" >/dev/null 2>&1 || true
    STASHED=0
  else
    warn "stash 还原有冲突, 本次改动仍完整保留在 $match, 请手动处理(不要 git stash clear)"
  fi
}
restore_stash() { restore_one_stash; }

BACKUP_BRANCH=""

cleanup_backup() {
  if [[ -n "$BACKUP_BRANCH" ]] && git show-ref --verify -q "refs/heads/$BACKUP_BRANCH"; then
    run git branch -D "$BACKUP_BRANCH"
  fi
}

restore_on_failure() {
  local rc=$?
  trap - EXIT
  git rebase --abort >/dev/null 2>&1 || true
  git merge --abort >/dev/null 2>&1 || true
  git checkout -f "$WORK_BRANCH" >/dev/null 2>&1 || true
  if [[ -n "$BACKUP_BRANCH" ]] && git show-ref --verify -q "refs/heads/$BACKUP_BRANCH"; then
    if [[ "$(git branch --show-current)" == "$WORK_BRANCH" ]]; then
      git reset --hard "$BACKUP_BRANCH" >/dev/null 2>&1 || true
    else
      git reset --hard >/dev/null 2>&1 || true
      git branch -f "$WORK_BRANCH" "$BACKUP_BRANCH" >/dev/null 2>&1 || true
    fi
  else
    git reset --hard >/dev/null 2>&1 || true
  fi
  if (( STASHED )); then
    warn "中途失败(rc=$rc), 恢复现场与 stash 改动"
    restore_one_stash
  fi
  if [[ -n "$BACKUP_BRANCH" ]]; then
    warn "$WORK_BRANCH 已恢复到同步前状态, $BACKUP_BRANCH 保留作回滚锚点, 确认后 git branch -D $BACKUP_BRANCH"
  fi
  exit "$rc"
}

ensure_upstream() {
  if git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
    log "上游 remote $UPSTREAM_REMOTE 已存在: $(git remote get-url "$UPSTREAM_REMOTE")"
    return
  fi
  if [[ -z "$UPSTREAM_URL" ]]; then
    die "上游 remote $UPSTREAM_REMOTE 未配置且未提供 UPSTREAM_URL, 请在脚本顶部设置 UPSTREAM_URL"
  fi
  run git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
}

sync_main() {
  run git fetch --no-tags "$UPSTREAM_REMOTE" --prune
  run git fetch "$MAIN_REMOTE" --prune
  if (( DRY_RUN )); then
    log "(dry-run) 跳过主分支与上游比对/硬重置(需要真实 fetch 后的新 ref)"
    run git push --force-with-lease "$MAIN_REMOTE" "$MAIN_BRANCH"
    return
  fi
  local local_main upstream_main
  local_main="$(git rev-parse "$MAIN_BRANCH")"
  upstream_main="$(git rev-parse "$UPSTREAM_REMOTE/$MAIN_BRANCH")"
  if [[ "$local_main" == "$upstream_main" ]]; then
    log "主分支已与上游一致"
  else
    local ahead
    ahead="$(git rev-list --count "${UPSTREAM_REMOTE}/${MAIN_BRANCH}..${MAIN_BRANCH}")"
    if [[ "$ahead" -gt 0 ]]; then
      warn "本地 $MAIN_BRANCH 有 $ahead 个 fork 独有提交, 将被 reset --hard 丢弃(reflog 可找回)"
    fi
    run git checkout "$MAIN_BRANCH"
    run git reset --hard "$UPSTREAM_REMOTE/$MAIN_BRANCH"
  fi
  run git push --force-with-lease "$MAIN_REMOTE" "$MAIN_BRANCH"
}

sync_work() {
  if ! git show-ref --verify -q "refs/heads/$WORK_BRANCH"; then
    if git ls-remote --exit-code --heads "$MAIN_REMOTE" "$WORK_BRANCH" >/dev/null 2>&1; then
      run git checkout -b "$WORK_BRANCH" "$MAIN_REMOTE/$WORK_BRANCH"
    else
      run git checkout -b "$WORK_BRANCH"
      run git push -u "$MAIN_REMOTE" "$WORK_BRANCH"
    fi
    return
  fi
  run git checkout "$WORK_BRANCH"
  run git fetch "$MAIN_REMOTE" --prune
  if git ls-remote --exit-code --heads "$MAIN_REMOTE" "$WORK_BRANCH" >/dev/null 2>&1; then
    [[ "$(git rev-parse "$WORK_BRANCH")" == "$(git rev-parse "$MAIN_REMOTE/$WORK_BRANCH")" ]] \
      || die "本地 $WORK_BRANCH 与 $MAIN_REMOTE/$WORK_BRANCH 不一致, 先手动处理再同步"
  else
    run git push -u "$MAIN_REMOTE" "$WORK_BRANCH"
  fi
  [[ "$(git rev-parse "$MAIN_REMOTE/$WORK_BRANCH")" == "$(git rev-parse "$MAIN_BRANCH")" ]] && { log "工作分支已与主分支一致"; return; }

  local backup="${WORK_BRANCH}-backup"
  run git branch -f "$backup" "$WORK_BRANCH"
  BACKUP_BRANCH="$backup"
  local base unique
  base="$(git merge-base "$WORK_BRANCH" "$MAIN_BRANCH")"
  unique="$(git rev-list --count "$base..$WORK_BRANCH")"
  [[ "$unique" -eq 0 ]] && { log "无独有提交, 快进到主分支"; run git reset --hard "$MAIN_BRANCH"; cleanup_backup; return; }

  if [[ "$SYNC_MODE" == "rebase" ]]; then
    log "$WORK_BRANCH 有 $unique 个独有提交, [rebase] 重放到 $MAIN_BRANCH 之后(逐个保留, 历史重写, 需强推)"
    run git rebase "$MAIN_BRANCH"
    run git push --force-with-lease "$MAIN_REMOTE" "$WORK_BRANCH"
    run git reset --hard "$MAIN_REMOTE/$WORK_BRANCH"
  else
    log "$WORK_BRANCH 有 $unique 个独有提交, [merge] 把 $MAIN_BRANCH 合并进来(原提交 SHA 不变, 不重写历史, 普通推送)"
    if git merge-base --is-ancestor "$MAIN_BRANCH" "$WORK_BRANCH"; then
      log "$MAIN_BRANCH 已包含在 $WORK_BRANCH 中, 无需合并"
    else
      run git merge "$MAIN_BRANCH" --no-edit -m "merge: 同步 $MAIN_BRANCH 到 $WORK_BRANCH"
    fi
    run git push "$MAIN_REMOTE" "$WORK_BRANCH"
  fi
  cleanup_backup
}

main() {
  cd_to_git_root
  [[ -n "$(git branch --show-current)" ]] || die "detached HEAD, 先切到分支"
  [[ "$SYNC_MODE" == "merge" || "$SYNC_MODE" == "rebase" ]] || die "非法 SYNC_MODE=$SYNC_MODE, 只支持 merge | rebase"

  local start_branch
  start_branch="$(git branch --show-current)"
  log "上游=$UPSTREAM_REMOTE fork=$MAIN_REMOTE 主分支=$MAIN_BRANCH 工作分支=$WORK_BRANCH 模式=$SYNC_MODE dry_run=$DRY_RUN"

  trap restore_on_failure EXIT
  ensure_upstream
  stash_workdir
  sync_main
  sync_work
  trap - EXIT
  restore_stash
  log "完成, 当前在 $(git branch --show-current)"
}

main "$@"
