# Decompile: {func_name}

## Goal

Write C code that compiles to a 100% binary-identical match with the target assembly.

## Available Files

| File | Description |
|------|-------------|
| `base.c` | m2c decompilation output (starting point) |
| `template.c` | Reference code from the most similar already-matched function |
| `similar_functions.txt` | List of top 5 similar decompiled functions (for additional references) |
| `target.s` | Target PowerPC assembly |
| `context.txt` | Type definitions and function prototypes (symlink) |
| `build.sh` | Compile and check match percentage |
| `match_log.txt` | History of match attempts |

## Workflow

1. **Read first**: Read `target.s` (the assembly you must match) and `template.c` (reference code from a similar matched function). Understand the function's structure before writing code. If `template.c` doesn't help, check `similar_functions.txt` for other reference functions you can look up.
2. **Start from base.c**: The m2c output gives you a skeleton. Create `base_1.c` with improvements based on the template and assembly.
3. **Build and check**: Run `./build.sh base_1.c`. It prints match percentage and a diff.
4. **Iterate**: Read the diff output carefully. Create `base_2.c` fixing the specific differences shown. Each file should fix specific issues from the diff.
5. **Stop conditions**:
   - **100% match**: You're done. Report success.
   - **3 consecutive attempts with no improvement**: Try a fundamentally different approach (different loop structure, different variable types, restructured control flow). If you've exhausted alternative approaches, stop.
   - **Compilation keeps failing**: Fix syntax, don't keep repeating the same error.
6. **IMPORTANT — Always submit your best work**: Before stopping, make sure your highest-scoring compiling attempt has been run through `./build.sh`. If you've been iterating and your best score was on `base_3.c` but you broke things in `base_4.c`, go back and re-submit `base_3.c` or create a new `base_5.c` based on your best version. Never give up without at least one compiling attempt submitted. Even partial progress helps — your work will be reviewed and built upon.

## Critical Rules

- **C89 only**: All variables MUST be declared at the start of each block. No mid-block declarations.
- **Float literals**: `1.0F` (uppercase F). Never `1.0f`.
- **Hex literals**: `0xABC` (lowercase x, uppercase hex digits).
- **Field offsets**: The template function may access DIFFERENT fields than your target. Match the offsets in `target.s` exactly. `lwz r3, 0x12C(r4)` means offset 0x12C — find the right field name for that offset.
- **Do NOT modify** any files outside this sandbox directory.
- **Do NOT use** ninja, verify, or any commands outside the sandbox.

## Assembly-to-C Reference

### Basic Load/Store
| Assembly | C Code | Notes |
|----------|--------|-------|
| `bl FunctionName` | `FunctionName();` | Function call |
| `lwz rN, OFF(rB)` | `var = struct->field;` | Load 32-bit word |
| `stw rN, OFF(rB)` | `struct->field = var;` | Store 32-bit word |
| `lfs/stfs` | float field access | Use `F` suffix on literals |
| `lbz/stb` | `u8`/`s8` field access | |
| `lhz/sth` | `u16`/`s16` field access | |
| `lwzu/stwu` in loop | `*ptr++` | Load/store with pointer update |

### Comparisons & Branches
| Assembly | C Code | Notes |
|----------|--------|-------|
| `cmpwi rN, VAL` + `beq/bne` | `if (var == VAL)` | Compare and branch |
| `fcmpo` + `cror 2, 1, 2` | `if (f1 <= f2)` | Float <= (CR OR combines bits) |
| `cntlzw` + `rlwinm r0,r0,27,5,31` | `(var == 0)` | Branchless boolean |
| `clrlwi r3, r3, 24` | `(u8)var` or `var & 0xFF` | Clear high 24 bits |

### Integer Operations
| Assembly | C Code | Notes |
|----------|--------|-------|
| `subf r3, r4, r5` | `r5 - r4` | **Operands REVERSED** |
| `neg r3, r3` | `-var` | Arithmetic negation |
| `andc r3, r4, r5` | `r4 & ~r5` | AND with complement |
| `nor r3, r4, r4` | `~r4` | NOT via NOR with self |
| `srawi` + `addze` | `var / (1 << N)` | Signed divide by power of 2 |
| `mtctr` + `bdnz` | `do { } while(--n > 0);` | Count loop |

### Type Conversions
| Assembly | C Code | Notes |
|----------|--------|-------|
| `lbz` + `extsb` | `(s8)byte_var` | Sign-extend unsigned byte |
| `fctiwz` + `stfd` + `lwz` | `(s32)float_var` | Float to int via stack |
| `fneg f1, f1` | `-float_var` | Float negation |
| `lfs` from `.sdata2` | `1.0F` literal | Single precision |
| `lfd` from `.rodata` | `1.0` literal | Double precision (no F) |

### Bitfields & Switches
| Assembly | C Code | Notes |
|----------|--------|-------|
| `rlwimi r3, r4, SH, MB, ME` | `struct.bitfield = val;` | Rotate and insert bits |
| `rlwinm` with mask | `if (struct.bitfield)` | Extract and test bits |
| `slwi` + `lwzx` + `bctr` | `switch(var)` | Jump table (contiguous cases) |
| Series of `cmpwi` + `beq/bgt` | `switch(var)` | Binary search (sparse cases) |

### Misc
| Assembly | C Code | Notes |
|----------|--------|-------|
| `crclr 6` before `bl` | Variadic call | Function has `...` in prototype |

## Troubleshooting by Match Percentage

| Range | Problem | Fix |
|-------|---------|-----|
| **< 80%** | Wrong control flow | Restructure: different loop type, branch order, switch vs if-chain |
| **80-95%** | Expression or type mismatch | Fix casts, expression order, signed vs unsigned, int vs s32 |
| **95-99%** | Declaration order | Swap variable declaration order at top of function |
| **99%+** | Register allocation | Try `x += y` vs `x = x + y`, reorder declarations, compound assignment |
| **Compile error** | C89 violation | Move declarations to block start, fix types |

## Register Allocation (99%+ Fixes)

**The r31 rule**: `r31` is the first non-volatile register assigned. If your variable is in `r30` but target has `r31`, you're missing a variable declaration or an inline hoisted a variable.

**Fixing register swaps**:
- Swap declaration order at top of function
- Toggle `x = x + y` vs `x += y`
- Try `if ((var = func()) == 0)` vs separate assignment
- Try `x = y = z = 0` chained init vs separate assignments

**`int` vs `s32`**: `s32` is `long`. Using `int` can change register allocation. If registers are swapped after a function call, try changing argument types.

## Code Quality Rules (Avoid These Patterns)

### Partial Match Policy
**Target 50% match or higher.** Functions below 50% should remain as stubs. For non-100% matches, add a TODO comment:

```c
// @TODO: Currently 97.29% match - needs minor register allocation fix
void grVenom_80204284(Ground_GObj* gobj)
```

### Union Variants
When accessing union fields like `xDD4_itemVar` or `gv`, use the **correct variant for the file**:
- In `itbombhei.c`: use `ip->xDD4_itemVar.bombhei.field`
- In `grbigblue.c`: use `gp->gv.bigblue.field`

m2c picks the FIRST matching variant alphabetically, which is often WRONG. If you see `gv.corneria` in a bigblue file, that's an m2c artifact — fix it.

### No Local Struct Definitions
**NEVER define structs like `typedef struct itFoo_ItemVars { ... }` in your C file.**

If a struct doesn't exist:
1. Leave a comment noting the struct is needed
2. Use the closest existing struct or void* temporarily
3. The integration phase will add the struct properly

Defining local structs causes maintenance problems and may hide missing union variants.

### No Type Punning for Fake Matches
**NEVER use patterns like `*(f32*)&ip->field` to force a match.**

If a struct field has the wrong type:
- Leave a `/// FAKE MATCH: field should be f32` comment
- Or leave the function unmatched with a `/// #func_name` stub

Type punning creates fragile matches that break when structs are properly fixed.

### No Raw Offset Access
**NEVER use `*(s32*)((u8*)&ip->xDD4_itemVar + 0x50)` patterns.**

This indicates a missing struct field. Leave a comment noting what field is needed at what offset.

### No Sandbox Metadata Comments
**Strip all sandbox/m2c metadata comments before integration:**
- `// Decompilation of X`
- `// Unit: main/melee/...`
- `// m2c decompilation of X`

These comments are for sandbox tracking only and should never appear in the final source.

### Typedefs at Top of File
**All `typedef struct` definitions must be at the top of the file**, after includes and extern declarations, before any function definitions. Never define structs mid-file between functions.

### Control Flow Patterns
**Use structured control flow instead of goto patterns:**

**Converting do-while with goto to for loop:**
```c
// BEFORE (bad)
var = 0;
do {
    if (condition) { goto next_iter; }
    // work
next_iter:
    var++;
} while (var < N);

// AFTER (good) - continue now goes to increment
for (var = 0; var < N; var++) {
    if (condition) { continue; }
    // work
}
```

**Label-based boolean patterns**: When you see patterns like:
```c
skip = 0; goto check_skip;
// ...
check_skip:
if (skip == 0) { continue; }
```
Try refactoring to an inline function. If that breaks the match, add a comment explaining the logic:
```c
/* Note: This pattern is logically: skip = SomeCheck(var);
 * Inline function refactoring breaks match due to control flow. */
```

### Use Existing Helpers
Look for existing helper functions before writing verbose code:
- `itResetVelocity(ip)` instead of setting `.x = .y = .z = 0` manually
- `GET_ITEM(gobj)` instead of `gobj->user_data` with casts
- Check `template.c` for patterns used in similar functions

### Prefer Chain Assignments
For zeroing velocity, prefer:
```c
ip->x40_vel.x = ip->x40_vel.y = ip->x40_vel.z = 0.0F;
```
Over separate assignments (matches the original style better).

## Item Functions

Most item functions take `Item_GObj* gobj` and return `void` or `bool`. Standard pattern:

```c
void itFoo_SomeAction(Item_GObj* gobj)
{
    Item* ip = GET_ITEM(gobj);
    // ...
}
```

### Item Variables (xDD4_itemVar)

Each item has variables in a union at `ip->xDD4_itemVar`. Use the correct union field for the file:

```c
// In itbombhei.c: ip->xDD4_itemVar.bombhei.fieldName
// In ittaru.c:    ip->xDD4_itemVar.taru.fieldName
```

**m2c picks the first alphabetical union field** — often wrong. If you see `bombhei` in a different item file, that's wrong.

### Item Attributes (x4_specialAttributes)

Some items have read-only attributes accessed via:
```c
it<Name>Attributes* attr = ip->xC4_article_data->x4_specialAttributes;
```

If attributes code looks wrong (`temp->unk1C` instead of `attr->x1C`), the void* cast is missing.

## Common Mismatch Patterns

### Stack Size Mismatch

If the `stwu r1, -0xNN(r1)` offset differs between target and your code:

| Problem | Fix |
|---------|-----|
| Stack too big | Reuse variables, remove unused locals |
| Stack too small | Add `PAD_STACK(N)` after last variable |

```c
Item* ip = GET_ITEM(gobj);
PAD_STACK(8);  // Add 8 bytes to match target
```

### Struct Copying Mismatch

If diff shows `lwz/stw` but code generates `lfs/stfs`:

**Wrong** (field-by-field — generates typed instructions):
```c
pos->x = attrs->x4.x;
pos->y = attrs->x4.y;
pos->z = attrs->x4.z;
```

**Right** (whole struct — generates word copies):
```c
*pos = attrs->x4;
```
