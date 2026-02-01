# Melee Decompilation Project - User Guide

**NOTE: This file is for HUMAN users only. Claude should NOT read this file - it's for you to understand what's available and how things are organized.**

## Directory Structure

```
~/melee-decomp/
├── melee/              Main decompilation repository (git repo)
├── permuter/           decomp-permuter tool for register allocation
├── tools/              Python tooling for decompilation workflow
│   ├── tools.py        Main tool CLI
│   ├── vacuum.py       Autonomous decompilation system
│   ├── quality_check.py Code quality checker
│   └── ...
├── docs/               Documentation
│   ├── CLAUDE.md       Claude's dispatch/pointer document
│   ├── MANUAL_DECOMPILATION.md  Detailed manual workflow guide
│   ├── VACUUM_GUIDE.md          Vacuum autonomous system guide
│   ├── PR_REVIEW.md             Pre-submission checklist
│   └── USER.md                  This file
└── .worktrees/         Git worktrees for isolated work (created as needed)
```

## Available Workflows

### 1. Manual Decompilation (Classic)

**What it is**: Claude works directly on functions you specify, using the classic decompilation workflow.

**When to use**:
- Learning the decompilation process
- Working on specific targeted functions
- Need fine control over the process
- Tricky functions that need human-like reasoning

**How to invoke**:
```bash
# Just point Claude at a function:
"Decompile ftFox_SpecialN_StartAction using the manual workflow"
"Work on the item callbacks in itbox.c"
```

Claude will read MANUAL_DECOMPILATION.md and follow the phase-by-phase workflow.

### 2. Vacuum (Autonomous)

**What it is**: Fully autonomous system that picks functions, decompiles them in sandboxes, integrates matches, and submits PRs.

**When to use**:
- Bulk decompilation of similar functions
- You want to go AFK and come back to matches
- Functions with good reference material available

**How to invoke**:
```bash
cd ~/melee-decomp/melee
# Claude runs:
python3 ~/melee-decomp/tools/vacuum.py --max 10
```

See VACUUM_GUIDE.md for full details.

### 3. Hybrid (Vacuum + Manual Cleanup)

**What it is**: Vacuum generates sandbox matches, Claude manually integrates and fixes them.

**When to use**:
- Vacuum produced 50-99% matches that need tweaking
- Integration failed due to missing prototypes/headers
- Want to verify quality before committing

**How to invoke**:
"Check the vacuum sandboxes and manually integrate any good matches"

## Tool Overview

### Core Tools (tools.py)

| Command | Purpose | Speed |
|---------|---------|-------|
| `recommend` | Find functions with best reference material | ~10s |
| `similar <func> [--decompiled]` | Find similar functions | instant |
| `template <func>` | Get reference code from similar match | instant |
| `asm <func>` | Show target assembly | instant |
| `suggest <func>` | Analyze assembly for struct field types | instant |
| `m2c <func>` | Get m2c decompilation | ~0.1s |
| `scratch <func>` | Quick syntax check (NOT authoritative) | ~0.1s |
| `verify <func>` | Authoritative match verification | requires `ninja` first |
| `permute <func>` | Set up permuter for register allocation | ~1s setup |

### Vacuum Tools (tools.py)

| Command | Purpose |
|---------|---------|
| `vacuum-pick [N]` | Pick N best candidates for autonomous work |
| `sandbox <func>` | Create isolated sandbox for function |
| `integrate <func>` | Integrate sandbox match into source tree |
| `skip <func>` | Add function to difficult list |
| `skip --clean` | Remove matched functions from skip list |

### Analysis Tools (tools.py)

| Command | Purpose |
|---------|---------|
| `infer [--struct X]` | Infer types for unknown struct fields |
| `field-usage <struct> <field>` | Show all usages of a field |
| `source <unit>` | Get source file path for compilation unit |

### Quality Check (quality_check.py)

```bash
python3 ~/melee-decomp/tools/quality_check.py <file.c>
python3 ~/melee-decomp/tools/quality_check.py <function>
```

Detects common issues:
- Sandbox artifacts (m2c comments, temp variables)
- Pointer arithmetic instead of struct fields
- Generic parameter names (arg0, arg1)
- Type punning patterns
- Mid-file typedefs
- Goto patterns that should be loops
- And more...

Run this before submitting any PR!

## Documentation Guide for Humans

### When Claude Should Read Each Doc:

- **CLAUDE.md**: Always (loaded in context) - dispatch/pointer to other docs
- **MANUAL_DECOMPILATION.md**: When doing manual/"by hand" decompilation
- **VACUUM_GUIDE.md**: When working with vacuum system
- **PR_REVIEW.md**: Before submitting pull requests
- **USER.md**: NEVER - this is for you, not Claude

### Updating Documentation:

**IMPORTANT**: When you learn something new or reviewers complain about something:

1. **Tell Claude to update the docs**
2. Claude should ask which doc needs updating:
   - CLAUDE.md for critical universal rules
   - MANUAL_DECOMPILATION.md for manual workflow insights
   - VACUUM_GUIDE.md for vacuum-specific knowledge
   - PR_REVIEW.md for new reviewer feedback patterns
3. Update immediately while knowledge is fresh

**The docs decay if not maintained.** Capture new knowledge right away.

## Project Status

As of February 2026:

- **Total functions**: ~X functions in codebase
- **Matched**: ~Y% complete
- **Recent work**: Ground/stage functions, item callbacks
- **Active branches**: (check with `git branch -a`)

To see current PR status:
```bash
cat ~/melee-decomp/tools/.submitted_prs
```

## Common Tasks

### Starting a New Session

```bash
# Create worktree for isolated work
cd ~/melee-decomp/melee
git worktree add ~/melee-decomp/.worktrees/my-feature -b decomp/my-feature

# Set environment (so tools use worktree)
export MELEE_REPO=~/melee-decomp/.worktrees/my-feature

# Tell Claude:
"I've created a worktree at ~/melee-decomp/.worktrees/my-feature. Let's work on <task>."
```

### Cleaning Up After Session

```bash
# If successful (merged PR):
cd ~/melee-decomp/melee
git worktree remove ~/melee-decomp/.worktrees/my-feature
git branch -D decomp/my-feature  # if merged to remote

# If abandoned:
git worktree remove --force ~/melee-decomp/.worktrees/my-feature
git branch -D decomp/my-feature
```

### Checking Function Status

```bash
# See what's left in a file:
cd ~/melee-decomp/melee
python3 ../tools/tools.py funcs <unit_name>

# Find good candidates:
python3 ../tools/tools.py recommend
```

### Running Vacuum

```bash
# Pick candidates and run autonomously:
cd ~/melee-decomp/melee
python3 ../tools/vacuum.py --max 10

# Or let Claude drive it:
"Run vacuum on up to 10 functions"
```

## Tips and Tricks

### Use Embeddings for Discovery

The `recommend` command uses embeddings to find functions with the best reference material. This is usually the best starting point.

```bash
python3 ~/melee-decomp/tools/tools.py recommend
```

### Verify Before Trusting scratch

`scratch` is fast but can show lower percentages than actual match (missing header inlines). Always run `ninja` + `verify` for authoritative results.

### Permuter for Final Push

If you're at 99%+ match (score < 1000), permuter can often find the perfect match automatically. Don't use it below 99% - fix structure first.

### Quality Check Everything

Before submitting any PR:
```bash
python3 ~/melee-decomp/tools/quality_check.py <file.c>
```

This catches most issues reviewers will flag.

### Track Your PRs

Update `.submitted_prs` when you create a PR to avoid duplicate work:
```bash
echo "branch | function | file | pending" >> ~/melee-decomp/tools/.submitted_prs
```

## Getting Help

### From Claude:

- "Explain how to use <tool>"
- "What's the difference between scratch and verify?"
- "Why is my match not improving?"
- "Read MANUAL_DECOMPILATION.md and help me decompile <function>"

### From the Community:

- Check project README and CONTRIBUTING.md
- Ask in Discord/forum (if available)
- Look at recent PRs for examples

## Environment Setup

Make sure these are set when working:

```bash
# Point tools at your worktree (not main repo)
export MELEE_REPO=~/melee-decomp/.worktrees/my-feature

# Optional: Add tools to PATH
export PATH="$PATH:$HOME/melee-decomp/tools"

# Or create aliases:
alias mtools='python3 ~/melee-decomp/tools/tools.py'
alias mvacuum='python3 ~/melee-decomp/tools/vacuum.py'
alias mquality='python3 ~/melee-decomp/tools/quality_check.py'
```

## Troubleshooting

### "verify shows 100% but CI says no match"

`MELEE_REPO` not set. Tools read the wrong report.json.

**Fix**: `export MELEE_REPO=/path/to/worktree` and re-run `ninja` + `verify`.

### "Tools can't find the melee repo"

Update paths in the tools or set MELEE_REPO.

Most tools default to `~/melee-decomp/melee` now, but verify if something fails.

### "Quality check shows errors I don't understand"

Read PR_REVIEW.md - it explains every pattern the quality checker flags and how to fix them.

### "Vacuum integrated a bad match"

Check `~/melee-decomp/tools/vacuum.log` for what happened. You can:
1. Manually fix the code
2. Revert the integration and mark function as difficult
3. Update vacuum.py if it's a systemic issue

---

**Remember**: This file (USER.md) is for YOUR reference. Claude should NOT read it during normal operation - it's context pollution for tasks. The other docs (CLAUDE.md, MANUAL_DECOMPILATION.md, etc.) are what Claude uses.
