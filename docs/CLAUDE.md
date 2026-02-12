# CLAUDE.md

LLM-assisted decompilation of Super Smash Bros. Melee (GameCube, US v1.02). Convert PowerPC assembly to C that compiles to 100% binary-identical match using mwcc.

**CRITICAL RULES:**
1. **NEVER modify files in `~/melee-decomp/melee` directly** - ALWAYS use git worktrees
2. **ALWAYS set `MELEE_REPO`** environment variable when working in worktrees or `verify` reads wrong report.json
3. **NEVER read USER.md** - it's for humans only, not relevant to your tasks
4. **When replying to GitHub PR comments**, use quote formatting (`>`) — you're not the user

## Documentation Map

**Read the appropriate doc based on task:**

| Task | Documentation to Read |
|------|----------------------|
| Manual/"by hand" decompilation | **MANUAL_DECOMPILATION.md** (MANDATORY) |
| Vacuum autonomous system | **VACUUM_GUIDE.md** |
| Before submitting PR | **PR_REVIEW.md** |
| User questions about workflow | **USER.md is OFF-LIMITS** - infer from task context |

## Tool Locations

All tools in: `~/melee-decomp/tools/`

```bash
python3 ~/melee-decomp/tools/tools.py <command>    # Main tooling
python3 ~/melee-decomp/tools/vacuum.py [options]   # Autonomous decompilation
python3 ~/melee-decomp/tools/quality_check.py <file>  # Code quality checker
```

Repo locations:
- Main repo: `~/melee-decomp/melee/` (never edit directly!)
- Worktrees: `~/melee-decomp/.worktrees/<name>/` (work here)
- Permuter: `~/melee-decomp/permuter/`

## Quick Command Reference

```bash
# Discovery
tools.py recommend               # Find best functions to work on
tools.py similar <func> --decompiled  # Find similar matched functions

# Analysis
tools.py template <func>         # Get reference code from similar match
tools.py asm <func>              # Get target assembly
tools.py suggest <func>          # Analyze assembly for struct field offsets

# Iteration
tools.py scratch <func>          # Quick check (NOT authoritative)
ninja && tools.py verify <func>  # AUTHORITATIVE verification

# Quality
quality_check.py <file.c>        # Catch issues before PR
git clang-format                 # Fix code style

# Comparison (like CI bot report)
tools.py compare <base_ref>     # Compare current build vs base (e.g. master)
tools.py compare --reports <before.json> <after.json>  # Compare two report files
```

## mwcc Compiler Constraints

- **ANSI C89**: All variables at block start, no C99 features
- **Float literals**: `1.0F` (uppercase F), never `extern f32 lbl_...`
- **Hex literals**: `0xABC` (lowercase x, uppercase digits)
- **Type sensitivity**: `int` vs `s32` affects register allocation
- **Expression order**: `a + b` vs `b + a` can change codegen
- **Declaration order**: Directly affects register allocation

### Pragmas

| Pragma | Purpose |
|--------|---------|
| `#pragma peephole on` | Re-enable peephole optimizer (asm blocks disable it) |
| `#pragma fp_contract on` | Enable `fmadds` (fused multiply-add) |
| `#pragma dont_inline on` | Prevent inlining |

**Critical bug**: Any `asm {}` block disables peephole for ALL subsequent functions. Fix: wrap function in `#pragma push` / `#pragma peephole on` / `#pragma pop`.

## Instruction-to-C Recipes

### Comparisons & Booleans

| Assembly Pattern | C Code | Notes |
|------------------|--------|-------|
| `fcmpo` + `cror 2, 1, 2` | `if (f1 <= f2)` | CR OR combines LT and EQ bits |
| `cntlzw r0, r3` + `rlwinm r0, r0, 27, 5, 31` | `bool b = (var == 0);` | Branchless boolean |
| `clrlwi r3, r3, 24` | `(u8)var` or `var & 0xFF` | Clear high 24 bits |

### Integer Operations

| Assembly Pattern | C Code | Notes |
|------------------|--------|-------|
| `srawi r0, r3, N` + `addze r3, r0` | `var / (1 << N)` | Signed div by power of 2 |
| `neg r3, r3` | `-var` | Arithmetic negation |
| `andc r3, r4, r5` | `r4 & ~r5` | AND with complement |
| `nor r3, r4, r4` | `~r4` | Bitwise NOT |
| `subf r3, r4, r5` | `r5 - r4` | Operands reversed |

### Type Conversions

| Assembly Pattern | C Code | Notes |
|------------------|--------|-------|
| `lbz` + `extsb` | `(s8)byte_var` | Sign-extend byte |
| `fctiwz` + `stfd` + `lwz` | `(s32)float_var` | Float to signed via stack |
| `bl __cvt_fp2unsigned` | `(u32)float_var` | Float to unsigned needs helper |
| `xoris` + `stw` + `stw` + `lfd` + `fsub` | `(float)int_var` | Int to float magic |

### Floating Point

| Assembly Pattern | C Code | Notes |
|------------------|--------|-------|
| `lfs` from `.sdata2` | `1.0F` literal | Single precision (use F suffix) |
| `lfd` from `.rodata` | `1.0` literal | Double precision (no suffix) |
| `fmr f1, f2` | Temp float or inline return | Float register move |
| `fneg f1, f1` | `-float_var` | Float negation |
| `lfs` for abs comparison | `__fabsf(x)` | Float-precision abs (vs `fabsf`/`fabs` which use `lfd`) |

### Bitfields, Loops, Switches

| Assembly Pattern | C Code | Notes |
|------------------|--------|-------|
| `rlwimi r3, r4, SH, MB, ME` | `struct.bitfield = val;` | Rotate and insert bits |
| `rlwinm` with mask | `if (struct.bitfield)` | Extract and test bits |
| `mtctr` + `bdnz` | `do { } while (--count > 0);` | Decrement-and-branch loop |
| `lwzu`/`stwu` in loop | `*ptr++ = val;` | Load/store with update (post-increment) |
| `stbu`/`stwu` single | `*++ptr = val;` | Store with update (pre-increment) |
| `slwi` + `lwzx` + `mtctr` + `bctr` | `switch(var)` contiguous cases | Jump table |
| Series of `cmpwi` + `beq`/`bgt` | `switch(var)` sparse cases | Binary search tree |

### Misc Patterns

| Assembly Pattern | C Code |
|------------------|--------|
| `crclr 6` before `bl` | Variadic function call (`...` in prototype) |
| `lwz rN, OFFSET(rBase)` / `stw` | Load/store word (struct field access) |
| `lfs`/`stfs` | Float field access |
| `lbz`/`stb` | u8/s8 field access |
| `lhz`/`sth` | u16/s16 field access |

## Register Allocation

**The r31 rule**: `r31` is first non-volatile register assigned. If your variable is in `r30` but target has `r31`, you're missing a variable declaration or an inline hoisted a variable.

**Fixing register swaps**: swap declaration order, toggle `x = x + y` vs `x += y`, use `if ((var = func()) == 0)` vs separate assignment, try `x = y = z = 0` chained init.

**Function call effects**: `r3-r10` are arguments. Swaps after calls may indicate wrong argument type (`int` vs `s32/long`).

## Stack Frame Debugging

**Too big**: unused inline (mwcc reserves stack for inline locals), hidden temporaries, `volatile` variable.
**Too small**: missing GET_FIGHTER/GET_ITEM macro (adds 4-8 bytes), missing inline call, missing struct/array (3 floats as `Vec3`).
**Gap detection**: If offsets jump from `8(r1)` to `24(r1)`, the 16-byte gap is likely a `Vec3` in an inlined helper.

### Stack Size Mismatch Fix

If the `stwu r1, -0xNN(r1)` offset differs from target:
- **Stack too big**: Reuse variables, remove unused locals
- **Stack too small**: Add `PAD_STACK(N)` after last stack variable

```c
Item* ip = GET_ITEM(gobj);
HSD_GObj* other = someCall(gobj);
PAD_STACK(8);  // Add 8 bytes to match target stack frame
```

### Struct Copying Mismatch

If diff shows `lwz/stw` but your code generates `lfs/stfs` (or vice versa):

**Wrong** (field-by-field):
```c
pos->x = attrs->x4.x;
pos->y = attrs->x4.y;
pos->z = attrs->x4.z;
```

**Right** (whole struct):
```c
*pos = attrs->x4;
```

The compiler copies structs word-by-word regardless of field types. m2c generates field-by-field copies which use typed instructions.

## Inline Wrappers

```c
#define GET_FIGHTER(gobj) ((Fighter*)HSD_GObjGetUserData(gobj))
#define GET_ITEM(gobj)    ((Item*)HSD_GObjGetUserData(gobj))
#define GET_JOBJ(gobj)    ((HSD_JObj*)HSD_GObjGetHSDObj(gobj))
// Alternative when macro causes mismatch:
Fighter* fp = gobj->user_data;
```

In `fighter.c`, `getFighter()` inline is required in first half but "hostile" in second half (use direct access).

## Item Functions

Most item functions take `Item_GObj* gobj` as first parameter and return `void` or `bool`. Standard pattern:

```c
void itFoo_SomeAction(Item_GObj* gobj)
{
    Item* ip = GET_ITEM(gobj);
    // ...
}
```

### Item Variables (xDD4_itemVar)

Each item type has variables in a union at `ip->xDD4_itemVar`. Access via correct union field:

```c
// In itbombhei.c:
ip->xDD4_itemVar.bombhei.fieldName
// In ittaru.c:
ip->xDD4_itemVar.taru.fieldName
```

Struct definitions in `src/melee/it/itCharItems.h` as `it<Name>_ItemVars`. If missing, define new struct there.

**m2c picks first alphabetical union field** — often wrong. If you see `bombhei` in a different item file, fix it.

### Item Attributes (x4_specialAttributes)

Some items have read-only attributes from `.dat` files accessed via:

```c
it<Name>Attributes* attr = ip->xC4_article_data->x4_specialAttributes;
```

Struct definitions in `src/melee/it/itCommonItems.h`. If missing, define new struct:

```c
typedef struct {
    u8 _pad[0x4];
    Vec3 x4_someVec;
    f32 x10_someFloat;
} itFooAttributes;
```

## Architecture

### Modules (two-letter prefixes)

`cm`=Camera, `db`=Debug, `ef`=Effects, `ft`=Fighters, `gm`=Game loop, `gr`=Ground/Stages, `if`=Interface/UI, `it`=Items, `lb`=Library, `mn`=Menu, `mp`=Map/Collision, `pl`=Player, `sc`=Scene, `ty`=Toys/Trophies, `vi`=Visual/Cutscenes

**Libraries**: `sysdolphin/baselib/` (HAL engine), `dolphin/` (SDK), `MSL/` (Metrowerks stdlib)

### Fighter Struct (~0x23EC bytes)

| Offset | Name | Purpose |
|--------|------|---------|
| `0x10C` | `ftData` | Pointer to fighter data |
| `0x2D4` | `specialAttributes` | Read-only attrs from .dat (MoveAttrs) |
| `0x2D8` | `specialAttributes2` | Character-specific attrs pointer |
| `0x222C` | `FighterVars` | **Union** - persistent per-fighter variables |
| `0x2340` | `MotionVars` | Temporary state, reset on action change |

`0x222C`-`0x2340` is a union. Each character defines its own struct (e.g., `FtMarioVars`). Access: `fp->sv.mario.varName`.

**GObj**: `HSD_GObj->user_data` (0x2C) points to `Fighter*`/`Item*`. `HSD_GObj->hsd_obj` points to `HSD_JObj*`.

### Data Section Ordering

`.init` → `.text` → `.ctors` → `.dtors` → `.rodata` → `.data` → `.bss` → `.sdata` → `.sbss` → `.sdata2`

- BSS variables ordered by **first usage**, not declaration
- mwcc pools identical float literals per file (decompiling out of order shifts float ordering)
- Data sections need `.balign 8` at file boundaries

## Code Quality

**Match percentage is necessary but NOT sufficient.** Prefer no match over a fake match.

**Rejected patterns**: raw array access (`data[x*5+y][0]`), generic names (`arg0`), no documentation, convoluted pointer math, type punning (`*(s32*)&float`), redundant casts, nonsensical assignment order, `BOOL`/`TRUE`/`FALSE` (use `bool`/`true`/`false`), bare `extern` in `.c` files (use headers), unnecessary casts (fix signatures instead).

**Also rejected (sandbox artifacts)**:
- `// Decompilation of X`, `// Unit: X`, `// m2c decompilation of` comments — strip before committing
- `typedef struct` definitions mid-file — move to top of file or appropriate header
- `goto next_iter` inside loops — convert to `for` loop with `continue`
- Label-and-goto for boolean checks — document with comment if inline function breaks match

### Control Flow Refactoring

**Converting do-while with goto to for loop with continue:**
```c
// BEFORE (bad - uses goto for loop continuation)
var = 0;
do {
    if (condition) { goto next_iter; }
    // work
next_iter:
    var++;
} while (var < N);

// AFTER (good - uses for loop with continue)
for (var = 0; var < N; var++) {
    if (condition) { continue; }
    // work
}
```

**Label-based boolean patterns**: If pattern can't be refactored to inline function without breaking match, add comment explaining logical equivalent.

### Partial Match Contribution Policy

**Contribute functions at 50% match or higher.** Let reviewers decide whether to keep or drop partial matches. Functions below 50% should remain as stubs (`/// #fn_ADDR`).

**Required for non-100% matches**: Add TODO comment above function indicating match percentage and what needs work:

```c
// @TODO: Currently 97.29% match - needs minor register allocation fix
void grVenom_80204284(Ground_GObj* gobj)
{
    // ...
}
```

Common TODO reasons:
- `needs register allocation fix` — variable order or type tweaks
- `needs control flow fix` — loop/branch structure differs
- `needs struct field fixes` — wrong struct layout or field access
- `below 50% threshold, needs significant work` — for functions wrapped in `#if 0`

## Naming Conventions

- **Function names must match file**: `itFreeze_*` for `itfreeze.c`, not legacy `it_3F14_*`
- **Non-static functions need module prefix**: `mnDiagram_*`, `grVenom_*`, etc.
- **Never use community-sourced names** (Uncle Punch, etc.) — derive from code analysis only
- **Symbol renames must update**: source, headers, AND `symbols.txt` (use `melee-replace-symbols`)
- **Struct definitions go in `module/types.h`**, not in `.c` files
- **Include style**: angle brackets for `baselib/`/`dolphin/` headers, quotes for project-local

## HAL Conventions

- `fp` = Fighter*, `gobj` = HSD_GObj*, `da`/`attr` = attributes pointer
- Suffixes: `_Anim` (animation), `_IASA` (input), `_Phys` (physics), `_Coll` (collision)
- Fighter folders use Japanese names: `ftMars`=Marth, `ftSeak`=Sheik, `ftKoopa`=Bowser
- Character codes: Ca=Falcon, Dk=DK, Fx=Fox, Mr=Mario, Lk=Link, Ss=Samus, Pk=Pikachu, Kb=Kirby, Ns=Ness, Pp=Popo/Nana, Pc=Pichu, Pr=Peach, Lg=Luigi, Ms=Marth, Zd=Zelda/Sheik, Ys=Yoshi, Dr=Dr.Mario, Fc=Falco, Cl=YoungLink, Gw=G&W, Mn=Mewtwo, Gn=Ganondorf, Fe=Roy, Kp=Bowser
- `PUSH_ATTRS(fp, AttrType)` macro copies special attributes from .dat to RAM (used in OnLoad functions)
- `OSReport`/`__assert` referencing a header file means the code is likely an inlined function from that header

## Troubleshooting

- **verify 100% but CI disagrees**: `MELEE_REPO` not set. Fix: `export MELEE_REPO=/path/to/worktree`, re-run `ninja` + `verify`.
- **Score not improving**: try different loop structures, reorder declarations, swap expression order, try `(s32)x` vs `(int)x`.
- **Permuter errors**: add `typedef int TypeName;` stubs, `void func();` prototypes, or `extern int global;` to base.c.
- **Epilogue scheduling**: "Build 167" scheduler bug. The patched `1.2.5n` compiler handles this.
- **Builtin inline unwanted**: If target calls `fmod`/`fabs`/etc. as `bl` (not inlined) but mwcc inlines it, add a forward declaration like `float fmod(float, float);` to suppress the builtin. This prevents mwcc from using its internal inline expansion, which can cascade into completely wrong FP register allocation.
- **Wrong field**: run `suggest` to see exact offsets, compare to struct definition in headers. Templates may use different fields.
- **Context file**: `~/melee-decomp/tools/context.txt` is used by `scratch` and `permute` automatically. `scratch` cannot handle header inlines — use `ninja` + `verify` instead.

## When Things Go Wrong

**If you encounter:**
- New compiler behavior not documented
- Reviewer complaints about patterns not covered in docs
- CI failures with unclear cause
- Tool errors or unexpected behavior
- Match issues not explained by existing troubleshooting

**ALWAYS do these steps:**

1. **Inform the user immediately**: "I encountered [X]. This should be documented."

2. **Press the user to update docs**: "Which doc should this go in?
   - CLAUDE.md (critical universal knowledge)
   - MANUAL_DECOMPILATION.md (manual workflow insight)
   - VACUUM_GUIDE.md (vacuum-specific knowledge)
   - PR_REVIEW.md (new reviewer pattern)"

3. **Capture it immediately**: Don't defer documentation. Knowledge decays quickly.

4. **Update sandbox_claude.md too**: If it affects vacuum, update `~/melee-decomp/tools/sandbox_claude.md` to keep it in sync.

**The docs must stay current with reality.** Every new issue is a docs bug that needs fixing.

## Experimental Pattern Tracking

Claude Code sessions accumulate decomp patterns in `memory/experimental-patterns.md` (in the Claude Code memory directory). After solving a non-trivial decomp problem:

1. Check if the pattern is already in experimental-patterns.md — if so, increment votes and add context
2. If it's new, add an entry with 1 vote
3. If a pattern reaches 3 votes, suggest promotion to the user

**Promotion** (user-approved only): Add the pattern to the appropriate section of this file AND sync to `tools/sandbox_claude.md`.

At the start of decomp work, skim experimental-patterns.md for potentially useful heuristics.

## Keeping Docs Synchronized

**IMPORTANT**: When adding decompilation knowledge to this file, also update `~/melee-decomp/tools/sandbox_claude.md` — that's what vacuum subagents see. Keep both in sync.

Vacuum agents don't see this file (CLAUDE.md), they see sandbox_claude.md. Critical patterns must be in both places.
