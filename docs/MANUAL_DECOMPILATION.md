# Manual Decompilation Guide

**READ THIS FIRST** when doing classic "by hand" decompilation workflow.

This guide contains the detailed phase-by-phase workflow for manual function decompilation. Use this when Claude is working directly on functions (not using vacuum).

## Tools Overview

| Tool | Purpose | Speed |
|------|---------|-------|
| `tools.py recommend` | Find functions with best reference material | ~10s |
| `tools.py similar <func>` | Find similar functions via embeddings | instant |
| `tools.py similar <func> --decompiled` | Find similar already-matched functions | instant |
| `tools.py template <func>` | Show C code from most similar matched function | instant |
| `tools.py m2c <func>` | Get m2c decompilation for stubbed functions | ~0.1s |
| `tools.py scratch <func>` | Quick syntax check (NOT authoritative) | ~0.1s |
| `tools.py permute <func>` | Set up permuter for regalloc fixes | ~50 iter/s/thread |
| `ninja` + `tools.py verify <func>` | **AUTHORITATIVE** match verification | ~2min |
| `configure.py --require-protos && ninja` | Pre-PR CI verification | ~2min |

All tools: `python3 ~/melee-decomp/tools/tools.py <command>`

### Analysis Tools

| Tool | Purpose |
|------|---------|
| `tools.py infer` | Infer types for unknown fields (x123) from usage patterns |
| `tools.py infer --struct Fighter` | Filter type inference to specific struct |
| `tools.py suggest <func>` | Analyze assembly to suggest field types |
| `tools.py field-usage <struct> <field>` | Show all usages of a struct field |

## IMPORTANT: scratch vs verify

**`verify` (after `ninja`) is the ONLY authoritative source of truth.**

- **scratch**: Compiles the function in isolation with context.txt. Fast but LIMITED.
  - ✓ Good for: Quick syntax checks, catching obvious structural issues
  - ✗ Cannot include: Header inlines (HSD_JObj*, GET_FIGHTER, etc.)
  - ✗ May show LOWER percentages than actual match (false negatives)

- **verify**: Reads from report.json after full `ninja` build. AUTHORITATIVE.
  - ✓ Uses complete header chain and all inlines
  - ✓ Shows actual match percentage
  - ✗ Requires full rebuild (~2min)

**Workflow**: Use `scratch` for quick iteration, but ALWAYS run `ninja` + `verify` before considering anything done.

## Phase 0: Session Setup (Git Worktree)

**CRITICAL: Always start a new decompilation session in an isolated git worktree.**

Git worktrees let you work on multiple branches simultaneously without switching. Each worktree has its own working directory but shares the git history.

```bash
# Create a new worktree for your decompilation session
cd ~/melee-decomp/melee
BRANCH_NAME="decomp/$(date +%Y%m%d)-session"  # Or use a descriptive name like "decomp/ftKirby-specials"
git worktree add ~/melee-decomp/.worktrees/my-feature -b "$BRANCH_NAME"

# Set environment so tools.py uses the worktree
export MELEE_REPO=~/melee-decomp/.worktrees/my-feature

# Work in the worktree
cd $MELEE_REPO

# Build the project in the worktree
ninja
```

**Why use worktrees:**
- Isolates your work from the main repo
- Easy to abandon failed attempts (just delete the worktree)
- Can run permuter in one worktree while editing in another
- No risk of accidentally committing to master

**When done with a session:**
```bash
# If successful: push branch and create PR from main repo
cd ~/melee-decomp/melee
git push origin <branch-name>

# Clean up the worktree
git worktree remove ~/melee-decomp/.worktrees/my-feature

# If abandoned: just remove without merging
git worktree remove --force ~/melee-decomp/.worktrees/my-feature
git branch -D <branch-name>
```

**Using tools.py with worktrees:**
Set the `MELEE_REPO` environment variable to point tools at your worktree:

```bash
# Set for current session
export MELEE_REPO=~/melee-decomp/.worktrees/my-feature

# Or add to your shell profile for persistence
echo 'export MELEE_REPO=~/melee-decomp/.worktrees/my-feature' >> ~/.bashrc

# Now all tools.py commands work with the worktree
tools.py verify <function>    # Reads from worktree's report.json
tools.py scratch <function>   # Compiles against worktree's build
```

**Recommended workflow:**
1. Create worktree: `git worktree add ~/melee-decomp/.worktrees/my-feature -b decomp/my-feature`
2. Set env: `export MELEE_REPO=~/melee-decomp/.worktrees/my-feature`
3. Work in worktree: `cd $MELEE_REPO`
4. All tools now use the worktree automatically

## Phase 0.5: Check Submitted PRs

**CRITICAL: Before starting work on any function, check the submitted PRs tracking file:**
```bash
cat ~/melee-decomp/tools/.submitted_prs
```

This file tracks functions that have already been submitted in pull requests to avoid duplicate work. It persists across branch switches since it's untracked.

**When you submit a PR, update the tracking file:**
```bash
# Add a line for each function in your PR
echo "branch-name | function_name | file_path | pending" >> ~/melee-decomp/tools/.submitted_prs
```

## Phase 1: Find a Function to Work On

**Option A: Recommended functions** (best approach)
```bash
tools.py recommend               # Functions with best reference material nearby
```
This finds undecompiled functions that have very similar already-matched functions. These are the best candidates because you have reference implementations to learn from.

**Option B: Explore similar functions**
```bash
tools.py similar <func>              # Find functions similar to <func>
tools.py similar <func> --decompiled # Only show already-matched ones as references
```

The similarity search uses Voyage AI embeddings to find functions with similar assembly patterns.

## Phase 2: Setup & Analysis

```bash
tools.py template <function>     # Get reference code from most similar matched function
tools.py asm <function>          # Get target assembly
tools.py source <unit>           # Find source file to edit
tools.py suggest <function>      # Analyze assembly for field types
```

The `template` command is the key tool here - it shows you C code from the most similar matched function along with the target assembly, giving you a direct starting point.

**CRITICAL: Verify field offsets!** The `suggest` command shows exact offsets accessed (e.g., `0x12C`). Always cross-reference these with struct definitions in header files. Similar functions may access DIFFERENT fields - don't assume the template's field names are correct for your target.

## Phase 3: Initial Decompilation

Write C code based on assembly patterns:
- `bl FunctionName` → `FunctionName();`
- `lwz rN, OFFSET(rBase)` → `var = struct->field;`
- `stw rN, OFFSET(rBase)` → `struct->field = var;`
- `lfs/stfs` → float field access
- `lbz/stb` → u8/s8 field access
- `lhz/sth` → u16/s16 field access

**Field offset verification workflow:**
1. Run `tools.py suggest <func>` to see offsets accessed (e.g., `0x12C | lfs | float`)
2. Find the struct definition in the header file (e.g., `types.h`)
3. Match the offset to the actual field name (e.g., `/* +12C */ float specialn_kp_flame_scale;`)
4. Use the CORRECT field name in your code, not the template's field name

**Example mismatch scenario:**
- Template uses `fp->x2340_stateVar1.foxVars.x0` at offset 0x2340
- But `suggest` shows your function accesses 0x2344
- Your code should use `fp->x2340_stateVar1.foxVars.x4`, not x0!

**C89 Requirements:**
- ALL variables declared at start of block (not mid-function)
- Float literals: `1.0F` (uppercase F)
- Hex literals: `0xABC` (lowercase x, uppercase digits)
- No C99 features (no `//` comments in .c files, use `/* */`)

## Phase 4: Fast Local Iteration

```bash
# Quick syntax/structure check (~0.1s per test)
tools.py scratch <function>
```

**WARNING**: scratch percentages may be LOWER than actual match for functions using header inlines (GET_FIGHTER, HSD_JObj* methods, etc.). Use it only for quick checks, not as source of truth.

Iterate on:
1. **Control flow**: `if/else` vs ternary, `while` vs `for` vs `do/while`
2. **Expression order**: `a + b` vs `b + a`, compound assignments
3. **Casts**: `(s32)x` vs `(int)x` (matters for register allocation!)
4. **Variable declaration order** (directly affects register allocation)
5. **Inline macro usage**: GET_FIGHTER vs direct access

**Common iteration patterns:**

### Control Flow Variations
```c
// Try these variations if match is close:
if (x) { a(); } else { b(); }
// vs
x ? a() : b();
// vs
if (!x) { b(); } else { a(); }
```

### Expression Order Sensitivity
```c
// mwcc is sensitive to evaluation order:
result = a + b + c;           // Different register usage than:
result = (a + b) + c;         // Different than:
result = a + (b + c);

// Also try:
x = x + y;                    // vs
x += y;
```

### Type Sensitivity
```c
// int vs s32 (s32 is long, affects codegen):
int count;                    // vs
s32 count;

// Especially in casts:
(int)float_var;               // vs
(s32)float_var;
```

**After making changes, run `ninja` + `verify` to check actual match.**

## Phase 5: Permuter (for register allocation)

**Only use when at 99%+ match** (score < 1000). For lower matches, fix control flow/structure first.

The permuter tries thousands of semantically-equivalent C variations to find one that matches the target assembly perfectly. It's a brute-force search over the space of:
- Variable declaration order
- Expression evaluation order
- Compound assignment transformations (`x = x + 1` vs `x += 1`)
- Temp variable introduction/elimination

```bash
# Set up permuter automatically
tools.py permute <function>

# Run permuter (parallel search with 24 threads)
cd ~/melee-decomp/permuter
python3 permuter.py nonmatchings/<function> -j24 --stop-on-zero
```

For long runs, use nohup to run in background:
```bash
nohup python3 permuter.py nonmatchings/<function> -j24 --stop-on-zero > permuter.log 2>&1 &
tail -f permuter.log  # Monitor progress
```

**If permuter finds a match:**
The improved code will be in `nonmatchings/<function>/best.c`. Review it for quality (no weird artifacts), then copy it back to the source file.

**If permuter gets stuck:**
- Check base.c for missing typedefs/prototypes
- Score might be too high (>1000) - fix structure first
- Function might have inline asm or compiler quirks permuter can't handle

## Phase 6: Final Verification

```bash
# Build the project (REQUIRED before verify)
cd $MELEE_REPO && ninja

# Verify against official report
tools.py verify <function>
```

Only commit when verify shows **100%**.

**If verify shows lower percentage than expected:**
1. Make sure `MELEE_REPO` is set correctly
2. Check that you ran `ninja` in the worktree, not main repo
3. Verify you're editing the right source file (use `tools.py source <unit>`)

## Phase 7: Pre-PR Verification

Before submitting a pull request, run the full CI verification suite:

```bash
cd $MELEE_REPO

# Run clang-format to fix code style
git clang-format

# Verify all functions have prototypes
python configure.py --require-protos && ninja

# Check for regressions
ninja diff
```

**Common CI failures:**
- "function has no prototype" → Add forward declaration in .c or .h file
- "clang-format check failed" → Run `git clang-format` and commit
- "regression in other file" → You changed a shared struct/header incorrectly

**Before submitting, also run:**
```bash
# Quality check your changes
python3 ~/melee-decomp/tools/quality_check.py <file.c>
```

See PR_REVIEW.md for full checklist of what reviewers look for.

## Quick Reference

```bash
# Discovery commands
tools.py recommend               # Best functions to work on
tools.py similar <func>          # Find similar functions
tools.py similar <func> --decompiled  # Find matched references
tools.py template <func>         # Get reference code

# Analysis commands
tools.py asm <func>              # Get target assembly
tools.py suggest <func>          # Suggest field types from assembly
tools.py source <unit>           # Get source file path
tools.py infer --struct Fighter  # Infer unknown field types

# Development commands
tools.py m2c <func>              # Get m2c decompilation
tools.py scratch <func>          # Quick test (NOT authoritative)
tools.py permute <func>          # Set up permuter
ninja && tools.py verify <func>  # AUTHORITATIVE verification

# Pre-PR commands
git clang-format                 # Fix code style
python configure.py --require-protos && ninja  # Verify prototypes
ninja diff                       # Check for regressions
python3 ~/melee-decomp/tools/quality_check.py <file>  # Quality check
```

## Troubleshooting

### Score not improving

Try these variations in order:
1. **Variable declaration order** - swap the order of local variable declarations
2. **Loop structure** - `for` vs `while` vs `do/while`
3. **Expression order** - `a + b` vs `b + a`, try parenthesizing differently
4. **Type casts** - `(int)x` vs `(s32)x` (s32 is long, affects codegen)
5. **Compound assignments** - `x = x + 1` vs `x += 1` vs `x++`
6. **Inline macros** - GET_FIGHTER vs direct `gobj->user_data`

### Wrong field accessed

Similar functions often access different struct fields. If your code looks correct but doesn't match:

1. Run `tools.py suggest <func>` to see the exact offsets (e.g., `0x12C | lfs | float`)
2. Find the struct definition in header files (grep for the struct name)
3. Match the hex offset to the field comment (e.g., `/* 0x12C */ float field_name;`)
4. The template function might use `field_a` at offset 0x124, but your target uses `field_b` at offset 0x12C

**Example:**
```bash
$ tools.py suggest ft_80082ABC
Offset | Inst | Type | Likely Field
0x12C  | lfs  | f32  | specialn_kp_flame_scale (Fighter*)
0x2D4  | lwz  | ptr  | specialAttributes (Fighter*)

# Now check the struct:
$ grep -n "0x12C" src/melee/ft/types.h
245:    /* 0x12C */ f32 specialn_kp_flame_scale;

# Your code should use:
fp->specialn_kp_flame_scale = ...;
// NOT whatever the template used!
```

### Stack frame size mismatch

If the `stwu r1, -0xNN(r1)` in your code differs from target:

**Stack too big** (your -0x50 vs target -0x40):
- Unused variable declared (compiler reserves space)
- Unnecessary temp variables
- Inline macro expanded differently than expected

**Stack too small** (your -0x30 vs target -0x40):
- Missing GET_FIGHTER macro call (adds 4-8 bytes)
- Missing local struct/array
- Need to add `PAD_STACK(N)` after variables:
  ```c
  Fighter* fp = GET_FIGHTER(gobj);
  HSD_GObj* other = someCall(gobj);
  PAD_STACK(8);  // Add 8 bytes padding
  ```

### Register allocation differences (99%+ match)

When you're at 99%+ match but registers are swapped (e.g., your code uses r30 but target uses r31):

**The r31 rule**: `r31` is the first non-volatile register assigned. If you're missing it:
- Missing a local variable declaration
- An inline function hoisted a variable to caller's frame

**Fixing register swaps:**
1. Swap variable declaration order (first declared → r31, second → r30, etc.)
2. Try different initialization patterns:
   ```c
   x = 0; y = 0;              // vs
   x = y = 0;                 // vs
   y = 0; x = 0;
   ```
3. Try `if ((var = func()) == 0)` vs separate assignment + check
4. Toggle `x = x + y` vs `x += y`

**Function call register effects:**
`r3-r10` are used for arguments, `r3-r4` for return values. Swaps after function calls often mean:
- Wrong argument type (`int` vs `s32`)
- Wrong number of arguments
- Inlined function vs real function call

### Permuter errors

If permuter fails to run:

**Error: "undefined reference to X"**
Edit `nonmatchings/<function>/base.c` to add:
```c
typedef int X;              // For unknown types
void X();                   // For unknown functions
extern int X;               // For unknown globals
```

**Error: "score too high" / no improvement**
- Score > 1000 means structural differences, permuter can't help
- Fix control flow, function calls, or struct access first
- Get to 99%+ before using permuter

**Permuter runs forever**
- Use `--stop-on-zero` to auto-stop on perfect match
- Check permuter.log for progress (should improve score every few minutes)
- If no improvement after 1 hour at high iteration count, might be stuck

### Verify shows 100% but CI disagrees

This means `MELEE_REPO` environment variable is not set, so `verify` read the wrong `report.json`.

**Fix:**
```bash
export MELEE_REPO=~/melee-decomp/.worktrees/my-feature
cd $MELEE_REPO
ninja
tools.py verify <function>
```

Add `export MELEE_REPO=...` to your shell profile to make it persistent across sessions.

## Common Patterns Reference

See CLAUDE.md for the full instruction-to-C recipe table covering:
- Comparisons and booleans (fcmpo, cror, cntlzw patterns)
- Integer operations (srawi, neg, andc, subf)
- Type conversions (float<->int, sign extension)
- Floating point (lfs/lfd from data sections)
- Bitfields and rotates (rlwimi, rlwinm)
- Loops (bdnz, ctr register)
- Switch statements (jump tables vs binary search)

## When Things Go Wrong

**If you encounter:**
- New compiler behavior not documented here
- Reviewer feedback about patterns you didn't know were wrong
- CI failures you don't understand
- Tools behaving unexpectedly

**ALWAYS:**
1. Document the issue and solution in the appropriate doc file
2. Update CLAUDE.md if it's critical knowledge for all workflows
3. Update this file if it's specific to manual decompilation
4. Update PR_REVIEW.md if it's something reviewers care about

**The docs must stay current with reality.** When you learn something new, capture it immediately.
