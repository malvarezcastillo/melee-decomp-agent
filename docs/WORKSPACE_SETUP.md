# Workspace Setup Reference

## Canonical Repository Location

**Main repo**: `~/melee-decomp-agent/melee`

This is the ONLY clone of the melee repository. All work should be done in worktrees created from this location.

## Quick Start for New Work

```bash
# 1. Navigate to main repo
cd ~/melee-decomp-agent/melee

# 2. Update to latest
git pull

# 3. Create a worktree for your feature
git worktree add ~/.worktrees/my-feature -b decomp/my-feature

# 4. Set MELEE_REPO and start working
export MELEE_REPO=~/.worktrees/my-feature
cd $MELEE_REPO
ninja
```

## Environment Variable

**CRITICAL**: Always set `MELEE_REPO` when working in a worktree, otherwise `verify` will read the wrong `report.json`:

```bash
export MELEE_REPO=/path/to/your/worktree
```

Consider adding this to your shell session when starting work.

## Cleanup After Work

```bash
# When done with a worktree
cd ~/melee-decomp-agent/melee
git worktree remove /path/to/worktree
git branch -D decomp/branch-name  # if not merged yet
```

## Current State

- **Worktrees**: 0 (clean slate)
- **Branches**: master only
- **Sandboxes**: 0 (vacuum artifacts cleaned)

## Worktree Conventions

- Use `~/.worktrees/` for organized worktree storage
- Name pattern: `decomp/<feature>` for regular work, `vacuum/<timestamp>` for vacuum work
- Always commit before running vacuum (it modifies the worktree)

## Tools Location

All tools run from: `python3 ~/melee-decomp-agent/melee-ai/tools.py <command>`

See `CLAUDE.md` for full tool reference and decompilation workflow.
