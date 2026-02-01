# Melee Decompilation Agent

LLM-assisted decompilation for Super Smash Bros. Melee using Claude Code.

## Overview

This system lets Claude Code iterate on decompiling Melee functions by:
1. Reading target PowerPC assembly
2. Writing C code to match
3. Compiling with the original mwcc compiler
4. Diffing against the target binary
5. Iterating until 100% match

## Prerequisites

The build system is already configured. Required tools are in `melee/build/`:
- `wibo` - Windows binary loader (runs mwcc on Linux)
- `mwcc` - MetroWerks CodeWarrior compiler (GC/1.3.2)
- `powerpc-eabi-objdump` - For disassembly
- `ninja` - Build system

## Usage

### Start a Decompilation Session

Tell Claude Code what to work on:

```
"Decompile grFZeroCar_801CAFBC"
```

```
"Let's work on the grfzerocar unit"
```

```
/melee-decompile grfzerocar
```

### Find What Needs Work

```bash
# Find the easiest functions to decompile (uses cache, instant)
python3 ~/melee-decomp-agent/melee-ai/tools.py easy

# Show more results
python3 ~/melee-decomp-agent/melee-ai/tools.py easy 50

# Rebuild cache (slow, ~2 min, needed after objdiff.json changes)
python3 ~/melee-decomp-agent/melee-ai/tools.py easy --refresh

# List incomplete units (sorted by function count)
python3 ~/melee-decomp-agent/melee-ai/tools.py list

# List functions in a unit
python3 ~/melee-decomp-agent/melee-ai/tools.py funcs main/melee/gr/grfzerocar
```

## Tools Reference

All tools are in `melee-ai/tools.py`:

| Command | Description |
|---------|-------------|
| `tools.py easy [N]` | Find easiest functions to decompile (cached) |
| `tools.py easy --refresh` | Rebuild the easy function cache |
| `tools.py list` | List incomplete units, sorted by function count |
| `tools.py funcs <unit>` | List functions in a unit |
| `tools.py asm <function>` | Get target assembly for a function |
| `tools.py source <unit>` | Get source file path for a unit |
| `tools.py ctx <unit>` | Build .ctx file (preprocessed headers) |
| `tools.py diff <unit>` | Build and diff against target |

## How "Easy" is Measured

The `easy` command ranks functions by complexity:

1. **Instruction count** - Fewer instructions = simpler logic
2. **Branch count** - Fewer branches = more linear code (easier to match)
3. **Call count** - Fewer calls = more self-contained

**Complexity score** = `instructions + (branches × 2)`

Functions are filtered to exclude:
- Gap/padding functions (0 instructions)
- Empty stubs (1 instruction, just `blr`)
- Functions without source files

**Ideal easy targets:**
- 2-10 instructions
- 0 branches (linear code)
- 0-1 function calls
- Simple patterns: getters, setters, struct copies

### Examples

```bash
# Get assembly for a function
python3 ~/melee-decomp-agent/melee-ai/tools.py asm lbTime_8000AEC8

# Check match percentage for a unit
python3 ~/melee-decomp-agent/melee-ai/tools.py diff main/melee/lb/lbtime

# Build context file for type lookups
python3 ~/melee-decomp-agent/melee-ai/tools.py ctx main/melee/ef/efsync
# Then grep the .ctx file for specific types:
# grep -A 20 "struct Fighter" melee/build/GALE01/src/melee/ef/efsync.ctx
```

## Workflow

```
┌─────────────────────────────────────────────────────────────┐
│  1. Find target: tools.py list                              │
│  2. Get assembly: tools.py asm <function>                   │
│  3. Get source path: tools.py source <unit>                 │
│  4. Build context: tools.py ctx <unit>                      │
│  5. Edit source file with C code                            │
│  6. Build and check: tools.py diff <unit>                   │
│  7. If not 100%, iterate on steps 5-6                       │
│  8. On 100% match: commit to melee repo                     │
└─────────────────────────────────────────────────────────────┘
```

## On Success

When a function matches 100%, Claude commits to the local melee repo:

```bash
cd ~/melee-decomp-agent/melee
git add src/path/to/file.c
git commit -m "Match function_name"
```

Check your progress anytime:
```bash
cd ~/melee-decomp-agent/melee
git log --oneline
```

## File Structure

```
melee-decomp-agent/
├── README.md                 # This file
├── melee/                    # melee-decomp repository
│   ├── src/                  # Source files to edit
│   ├── build/
│   │   ├── GALE01/
│   │   │   ├── src/          # Compiled objects + .ctx files
│   │   │   └── obj/          # Target objects (from original binary)
│   │   ├── binutils/         # powerpc-eabi-objdump, etc.
│   │   ├── compilers/        # mwcc compiler versions
│   │   └── tools/            # dtk, objdiff-cli
│   └── objdiff.json          # Unit configuration
└── melee-ai/
    └── tools.py              # CLI tools for Claude Code
```

## mwcc Compiler Notes

- **ANSI C89**: All variables must be declared at the start of blocks
- **Optimization**: `-O4,p` (aggressive optimization)
- Sensitive to: expression order, casts, ternary vs if/else, loop structure

## PowerPC Quick Reference

| Assembly | Meaning |
|----------|---------|
| `lwz rN, OFFSET(rBase)` | Load word from struct at OFFSET |
| `stw rN, OFFSET(rBase)` | Store word to struct at OFFSET |
| `bl FunctionName` | Call function |
| `blr` | Return |
| `r3-r10` | Function arguments |
| `r3` | Return value |
| `r31` | Often `this` or main struct pointer |
| `f1-f8` | Float arguments |
| `f1` | Float return value |

## Context Files

The `.ctx` files contain all preprocessed headers for a compilation unit. They're generated by `tools.py ctx <unit>` and stored in `melee/build/GALE01/src/.../*.ctx`.

These are the same format used by [decomp.me](https://decomp.me) for the "Context" field.

To look up a type:
```bash
grep -A 30 "struct Fighter {" melee/build/GALE01/src/melee/ft/fighter.ctx
```

## Troubleshooting

### Build fails
```bash
# Rebuild ninja configuration
cd melee && python3 configure.py --wrapper ~/.local/bin/wibo
```

### Missing tools
```bash
# Download all tools
cd melee && python3 -m ninja tools
```

### Check current progress
```bash
# See overall decomp progress
cd melee && python3 configure.py progress
```
