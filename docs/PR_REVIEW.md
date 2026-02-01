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
