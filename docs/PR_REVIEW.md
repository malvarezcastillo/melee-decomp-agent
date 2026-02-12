# PR Review Checklist

**READ THIS BEFORE SUBMITTING A PR** to catch issues that reviewers will flag.

This document lists all the patterns that reviewers look for and commonly reject. Run through this checklist before creating your pull request.

## Automated Quality Check

**ALWAYS run the quality checker before submitting:**

```bash
python3 ~/melee-decomp/tools/quality_check.py <file.c>
```

This catches most common issues automatically. Fix all errors and warnings before submitting.

## Code Quality Standards

### Match Percentage Requirements

- **100% match** - Ready to submit
- **99%+** - Consider using permuter first, or submit with clear TODO comment
- **50-99%** - Submit with `// @TODO: Currently X% match - needs Y` comment explaining what's needed
- **Below 50%** - Keep as stub (`/// #fn_ADDR`), not ready for PR

### Non-100% Match Comment Format

```c
// @TODO: Currently 97.29% match - needs minor register allocation fix
void grVenom_80204284(Ground_GObj* gobj)
{
    // ...
}
```

Common TODO reasons:
- `needs register allocation fix` - variable order or type tweaks
- `needs control flow fix` - loop/branch structure differs
- `needs struct field fixes` - wrong struct layout or field access
- `needs permuter` - at 99%+ but small diff remains

## Naming Conventions

### Function Names Must Match File

Function names must match the current file naming convention, not legacy split-file names:

```c
// ❌ BAD - legacy unsplit name in itfreeze.c
void it_3F14_Logic17_Destroyed(Item_GObj* gobj)

// ✓ GOOD - matches file name
void itFreeze_Destroyed(Item_GObj* gobj)
```

### Module Prefix Required on Non-Static Functions

All non-static functions must use the module prefix:

```c
// ❌ BAD - missing module prefix
void UpdateScrollArrows(HSD_GObj* gobj)

// ✓ GOOD - module prefix present
void mnDiagram_UpdateScrollArrows(HSD_GObj* gobj)
```

### No Community-Sourced Names

**Never pull names from Uncle Punch, training mode mods, or other community sources.** Names must be derived from code analysis only. If you can't determine the real name, use placeholder field names like `x2C` and document that naming is pending.

### Symbol Renames Must Be Comprehensive

When renaming functions, update ALL of:
1. Source files (`.c`)
2. Header files (`.h`)
3. `symbols.txt`

Use the `melee-replace-symbols` tool or `tools/functions.sh rename_tu` for mass renames.

## Rejected Patterns (Will Fail Review)

### 1. Sandbox Artifacts

**ALWAYS REMOVE** these before submitting:

```c
// ❌ BAD - Sandbox metadata comments
// Decompilation of it_802B0ABC
// Unit: itbox
// m2c decompilation of it_802B0ABC

// ✓ GOOD - Clean function with proper documentation
// Called when box item is grabbed by player
void itBox_OnGrab(Item_GObj* gobj)
```

Auto-detected by quality_check.py as ERROR.

### 2. Mid-File Typedefs

**MOVE TO TOP** of file or appropriate header:

```c
// ❌ BAD - typedef in middle of file
void someFunction() { ... }

typedef struct itBox_ItemVars {
    s32 field;
} itBox_ItemVars;

void anotherFunction() { ... }

// ✓ GOOD - typedef at top of file
typedef struct itBox_ItemVars {
    s32 field;
} itBox_ItemVars;

void someFunction() { ... }
void anotherFunction() { ... }
```

Better: Define in appropriate header (`itCommonItems.h`, `itCharItems.h`, etc.)

Auto-detected by quality_check.py as WARNING.

### 3. Goto for Loop Continuation

**USE CONTINUE** in proper for/while loop:

```c
// ❌ BAD - goto for loop continuation
var = 0;
do {
    if (condition) { goto next_iter; }
    // work
next_iter:
    var++;
} while (var < N);

// ✓ GOOD - proper for loop with continue
for (var = 0; var < N; var++) {
    if (condition) { continue; }
    // work
}
```

Auto-detected by quality_check.py as WARNING.

### 4. Pointer Arithmetic Instead of Struct Fields

**DEFINE PROPER STRUCT** fields:

```c
// ❌ BAD - raw pointer arithmetic
*(s32*)((u8*)ptr + 0x50) = value;
((ItemAttrs*)ptr)[5] = value;

// ✓ GOOD - proper struct field access
ip->xDD4_itemVar.box.someField = value;
```

Use `M2C_FIELD` as a fallback for unknown offsets (m2c `--valid-syntax` generates these automatically). But defining the actual struct field is always preferred.

Auto-detected by quality_check.py as ERROR.

### 5. Raw Array Access with Magic Numbers

**USE STRUCT TYPES** with named fields:

```c
// ❌ BAD - magic number array arithmetic
data[x * 5 + y][0] = value;

// ✓ GOOD - proper struct with meaningful name
player->attributes[slot_id].base_damage = value;
```

Auto-detected by quality_check.py as WARNING.

### 6. Generic Parameter Names

**USE DESCRIPTIVE NAMES** based on actual semantics:

```c
// ❌ BAD - generic names from m2c
void function(HSD_GObj* arg0, s32 arg1, f32 var1)

// ✓ GOOD - descriptive names
void function(Item_GObj* item_gobj, s32 damage_amount, f32 launch_speed)
```

Especially bad: `temp_r3`, `sp0C`, `var_f31` (m2c artifacts)

Auto-detected by quality_check.py as ERROR for m2c names, WARNING for arg0/var1.

### 7. Type Punning

**USE CORRECT TYPES**, don't force specific instructions:

```c
// ❌ BAD - type punning to force lwz instead of lfs
*(s32*)&float_field = some_int;

// ✓ GOOD - proper type usage (if this breaks match, struct definition is wrong)
float_field = some_float;
```

Exception: If marked with `// FAKE MATCH: ...` comment explaining the issue.

Auto-detected by quality_check.py as ERROR.

### 8. Redundant Cast Chains

**SIMPLIFY CASTS** to minimum necessary:

```c
// ❌ BAD - triple cast chain (fake match pattern)
result = (s32)(u32)(f32)value;

// ✓ GOOD - single cast (or fix types so no cast needed)
result = (s32)value;
```

Auto-detected by quality_check.py as WARNING.

### 9. Wrong Union Variant

**USE CORRECT VARIANT** for the file you're in:

```c
// In itbox.c:
// ❌ BAD - wrong union variant
ip->xDD4_itemVar.bombhei.timer = 0;  // bombhei in box file?

// ✓ GOOD - correct variant for this item
ip->xDD4_itemVar.box.timer = 0;

// In grvenom.c:
// ❌ BAD - wrong union variant
gv.corneria.someField = 0;  // corneria in venom file?

// ✓ GOOD - correct variant for this stage
gv.venom.someField = 0;
```

Auto-detected by quality_check.py as WARNING/INFO.

### 10. Missing Documentation

**DOCUMENT NON-TRIVIAL FUNCTIONS** (>5 lines):

```c
// ❌ BAD - complex function with no explanation
void itBox_80123456(Item_GObj* gobj)
{
    // 50 lines of complex code with no comments
}

// ✓ GOOD - explains purpose
// Handles box explosion when timer expires.
// Spawns item contents and triggers damage/knockback.
void itBox_OnExplosion(Item_GObj* gobj)
{
    // ...
}
```

Auto-detected by quality_check.py as INFO.

### 11. Unnecessary Casts

**FIX THE ROOT CAUSE** (function signatures, types) instead of adding casts:

```c
// ❌ BAD - casting to paper over type mismatch
Item_GObj_Callback cb = (void (*)(HSD_GObj*)) fn_802BB428;

// ✓ GOOD - fix fn_802BB428's declaration to match
Item_GObj_Callback cb = fn_802BB428;

// ❌ BAD - unnecessary cast when types already match
doSomething((HSD_GObj*) gobj);  // gobj is already Item_GObj*

// ✓ GOOD - no cast needed
doSomething(gobj);
```

### 12. Bare Extern Declarations in .c Files

**INCLUDE THE PROPER HEADER** instead of adding `extern` in source files:

```c
// ❌ BAD - bare extern in .c file
extern u8 un_804D6FFC;

// ✓ GOOD - include or create the header
#include "vi/types.h"
```

If a symbol is only referenced from one file, it may belong to that translation unit — check the splits.

### 13. Wrong Boolean Style

**USE `bool`/`true`/`false`**, not `BOOL`/`TRUE`/`FALSE`:

```c
// ❌ BAD - Windows-style macros
BOOL result = TRUE;
if (result == FALSE) { ... }

// ✓ GOOD - C99 stdbool style
bool result = true;
if (result == false) { ... }
```

### 14. Placeholder Prototype Removal

**NEVER remove `UNK_RET`/`UNK_PARAMS` placeholder prototypes from headers.** They preserve function ordering and satisfy `--require-protos`. Replace them with real prototypes when you implement the function, but don't delete them.

## Code Organization

### Struct Definitions Go in `module/types.h`

New struct definitions belong in the module's `types.h`, not in `.c` files or regular `.h` files:

```
src/melee/it/types.h     # Item types
src/melee/ft/types.h     # Fighter types
src/melee/gr/types.h     # Ground/Stage types
src/melee/mn/types.h     # Menu types
src/melee/cm/types.h     # Camera types
```

### Static Data Placement

Static data definitions (like `AnimLoopSettings`) go at the top of the file or in the `.static.h` file, not interleaved between functions.

### Inline Helpers in Shared Headers

Reusable `static inline` functions belong in shared headers, not local to one file. If a similar inline already exists in a header (e.g., `controller.h`), add yours next to it.

### Include Style

```c
// Angle brackets for library/baselib headers
#include <baselib/psstructs.h>
#include <dolphin/os/OSError.h>

// Quotes for project-local headers
#include "it/types.h"
#include "ft/forward.h"
```

### Use Project Macros

```c
// ❌ BAD - manual workarounds
int stack_pad = 0; (void)stack_pad;  // stack padding
int count = 25;                       // magic array size

// ✓ GOOD - project macros
PAD_STACK(4);
int count = ARRAY_SIZE(sp18);
```

## Style Requirements

### Float Literals

```c
// ❌ BAD - lowercase f
float x = 1.0f;
float y = 2.5f;

// ✓ GOOD - uppercase F
float x = 1.0F;
float y = 2.5F;
```

Auto-detected by quality_check.py as WARNING.

### Float Literals in Function Arguments

**Use proper float/pointer types in function call arguments**, not bare integers:

```c
// ❌ BAD - integer literals where floats/pointers are expected
Fighter_ChangeMotionState(gobj, 0x16A, 0, fp->cur_anim_frame, 1, 0, 0);

// ✓ GOOD - correct types
Fighter_ChangeMotionState(gobj, 0x16A, 0, fp->cur_anim_frame, 1.0F, 0.0F, NULL);
```

This is a common AI/LLM error — always check function prototypes for parameter types.

### Hex Literals

```c
// ❌ BAD - uppercase X or lowercase digits
int x = 0Xabc;
int y = 0x12ab;  // lowercase digits OK

// ✓ GOOD - lowercase x, uppercase hex digits
int x = 0xABC;
int y = 0x12AB;
```

Not auto-detected currently.

### NULL Checks for Pointers

```c
// Borderline - some reviewers prefer explicit NULL
if (!gobj) { ... }
if (!item_ptr) { ... }

// ✓ Safer - explicit NULL check
if (gobj == NULL) { ... }
if (item_ptr == NULL) { ... }
```

Auto-detected by quality_check.py as INFO for pointer-looking variables.

## Pre-Submit Checklist

Run these commands **in order** before creating PR:

### 1. Quality Check
```bash
python3 ~/melee-decomp/tools/quality_check.py <file.c>
```
**Must show**: No errors, preferably no warnings.

### 2. Code Formatting
```bash
git clang-format
```
**Must show**: No changes needed (or commit the formatting changes).

### 3. Prototype Verification
```bash
python configure.py --require-protos && ninja
```
**Must show**: Build succeeds with no "missing prototype" errors.

If you get "function has no prototype", add forward declaration:
```c
// In the .c file or corresponding .h header:
void myFunction(s32 arg);
```

### 4. Match Verification
```bash
export MELEE_REPO=~/melee-decomp/.worktrees/my-feature  # Your worktree path
cd $MELEE_REPO
ninja
tools.py verify <function>
```
**Must show**: 100% match (or expected % with TODO comment).

### 5. Regression Check
```bash
ninja diff
```
**Must show**: No unexpected changes in other files.

If you see changes in other files:
- You modified a shared struct/header incorrectly
- Or you fixed a bug in shared code (explain in PR description)

### 6. Visual Inspection

**Check the diff yourself:**
```bash
git diff
```

Look for:
- Sandbox comments (`// Decompilation of`, `// Unit:`, `// m2c decompilation`)
- m2c variable names (`temp_r3`, `sp0C`, `var_f31`)
- Generic names (`arg0`, `arg1`, `var1`)
- Mid-file typedefs
- Goto patterns inside loops
- Raw pointer arithmetic or array indexing with magic numbers

### 7. Documentation Check

For each function you modified:
- [ ] Has descriptive parameter names (not arg0, arg1)
- [ ] Non-trivial functions (>5 lines) have comment explaining purpose
- [ ] If <100% match, has `// @TODO: Currently X% match` comment
- [ ] If using unusual pattern, has comment explaining why
- [ ] Struct field names match offsets (verified with `tools.py suggest`)

## Common Reviewer Feedback

### "This looks like m2c output"

**Problem**: Code has m2c artifacts (temp_r3, raw arithmetic, generic names).

**Fix**:
1. Research what the code actually does (check callers, read assembly)
2. Use meaningful names based on semantics
3. Define proper struct fields for offset access
4. Add comments explaining purpose

### "Wrong field - check your offsets"

**Problem**: Used template's field name, but offset doesn't match target.

**Fix**:
1. Run `tools.py suggest <func>` to see actual offsets
2. Find struct definition in headers
3. Match hex offset to field name
4. Update code to use correct field

### "Add this struct to the header"

**Problem**: Local typedef should be in shared header.

**Fix**:
1. Move typedef to appropriate header:
   - Item vars → `src/melee/it/itCharItems.h` or `itCommonItems.h`
   - Ground vars → `src/melee/gr/types.h`
   - Fighter vars → `src/melee/ft/types.h`
2. Add to union if applicable (`itemVar`, `groundVar`, `FighterVars`)

### "This should use continue, not goto"

**Problem**: Loop uses `goto next_iter` for continuation.

**Fix**: Refactor to proper for loop with `continue` (see pattern #3 above).

### "Needs documentation"

**Problem**: Non-trivial function with no comment.

**Fix**: Add comment explaining:
- What the function does
- When it's called (on what event/state)
- Side effects (spawns objects, modifies state, etc.)

### "This is a fake match"

**Problem**: Code uses hacks (type punning, weird casts, nonsensical order) to force match.

**Fix**:
1. Try permuter if at 99%+ (it explores valid C variations)
2. If no valid match found, either:
   - Submit as <100% with TODO comment, OR
   - Leave as stub with `/// #fn_ADDR`, OR
   - If hack is necessary, add `// FAKE MATCH: <explanation>`

## When to Use FAKE MATCH Comments

**Only use when:**
1. You've exhausted all valid C variations (including permuter)
2. You understand WHY the hack is needed (compiler quirk, inline asm issue, etc.)
3. You document what the "real" code likely was

**Format:**
```c
// FAKE MATCH: Compiler appears to fuse these operations in a way C89 can't express.
// Real code likely used an inline asm block or compiler intrinsic.
// This type pun forces the correct instruction sequence.
*(s32*)&result = some_computation;
```

**Better**: Use permuter or submit as <100% instead of fake matching.

## PR Hygiene

### No Build Artifacts or Unrelated Changes

PRs must not include:
- `.build_validated` files
- `.gitkeep` changes
- Unrelated file modifications (revert accidental touches)

### No Regressions

The decomp-dev bot tracks match percentages. If your PR **breaks previously-matched functions**, you must fix the regressions before merge. Check the bot's report carefully.

### Fix Argument Types, Don't Cast Around Them

When a function parameter doesn't match the expected type:

```c
// ❌ BAD - cast workaround
void un_80321950(void* s)
{
    vi1202_UnkStruct* data = (vi1202_UnkStruct*)s;
    // ...
}

// ✓ GOOD - correct argument type
void un_80321950(vi1202_UnkStruct* s)
{
    // use s directly
}
```

## AI-Generated Code Notes

AI-generated code (Claude, Codex, etc.) receives the same review scrutiny as human code. Be transparent about AI usage in PR descriptions.

**Common AI errors reviewers catch:**
- Wrong union variant (m2c/LLM picks first alphabetical)
- Integer literals where float/pointer args expected (`0` instead of `0.0F` or `NULL`)
- Raw pointer arithmetic instead of struct fields
- `goto` patterns that should be `continue` in loops
- Nonsensical struct access through recast pointers
- Missing module prefix on function names

**When to prefer m2c over LLM**: For simple functions where the main challenge is type correctness, m2c with correct argument types often produces better results than an LLM. Use m2c `--valid-syntax` for clean output.

## Key Reviewers

Understanding reviewer focus areas helps prepare better PRs:

- **ribbanya**: Type system integrity, struct definitions in types.h, `bool`/`true`/`false` style, no community-sourced names, code organization
- **PsiLupan**: String access patterns, M2C_FIELD elimination, float literal correctness, item union types
- **r-burns**: Unnecessary casts, proper struct fields over M2C_FIELD, function signature fixes
- **sadkellz**: Camera struct correctness, speculative naming pushback, cross-referencing callers

## Final Reminders

**Match percentage is necessary but NOT sufficient.**

A 100% match with:
- Generic names (arg0, arg1)
- No documentation
- Obvious m2c artifacts
- Fake match patterns

...will be rejected.

**Prefer no match over a fake match.**

If you can't get a clean 100% match:
1. Try permuter (for 99%+ matches)
2. Submit as partial match with clear TODO comment (50%+)
3. Leave as stub if below 50%

**When in doubt, ask before submitting.**

Better to clarify expectations than waste time on a PR that needs major rework.
