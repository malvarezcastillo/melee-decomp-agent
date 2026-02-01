#!/usr/bin/env python3
"""
Simple CLI tools for Melee decompilation workflow.
Uses local mwcc compiler for compilation and m2c for decompilation.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# Optional dependencies for embedding features
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

def load_dotenv():
    """Load .env file from script directory if it exists."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

# Paths - MELEE_REPO can be overridden via environment variable for worktree support
MELEE_REPO = Path(os.environ.get("MELEE_REPO", Path(__file__).parent.parent / "melee"))
MELEE_AI = Path(__file__).parent
OBJDUMP = MELEE_REPO / "build/binutils/powerpc-eabi-objdump"
OBJDUMP_GNU = Path("/usr/bin/powerpc-linux-gnu-objdump")
OBJDIFF_JSON = MELEE_REPO / "objdiff.json"
SYMBOLS_TXT = MELEE_REPO / "config/GALE01/symbols.txt"
CONTEXT_TXT = MELEE_REPO.parent / "context.txt"

# Embedding feature paths and config
EMBEDDINGS_CACHE = MELEE_AI / ".embeddings_cache.json"
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-code-3"
VOYAGE_BATCH_SIZE = 64  # Max items per API request
VOYAGE_CONCURRENT_REQUESTS = 4  # Number of parallel API requests
VOYAGE_BATCH_CHAR_BUDGET = 150000  # Max chars per batch (~90k tokens, under 120k limit)
DISASSEMBLY_WORKERS = 8  # Parallel workers for objdump calls

# Local compiler tools
WIBO = MELEE_REPO / "build/tools/wibo"
SJISWRAP = MELEE_REPO / "build/tools/sjiswrap.exe"
MWCC = MELEE_REPO / "build/compilers/GC/1.2.5n/mwcceppc.exe"

# Compiler flags for Melee (GC MW 1.2.5n)
MWCC_FLAGS = [
    "-nowraplines", "-cwd", "source", "-Cpp_exceptions", "off",
    "-proc", "gekko", "-fp", "hardware", "-align", "powerpc",
    "-nosyspath", "-fp_contract", "on", "-O4,p", "-multibyte",
    "-enum", "int", "-nodefaults", "-inline", "auto",
    "-pragma", "cats off", "-pragma", "warn_notinlined off",
    "-RTTI", "off", "-str", "reuse", "-DBUILD_VERSION=0",
    "-DVERSION_GALE01", "-DM2CTX", "-DNDEBUG=1",
    "-maxerrors", "1", "-msgstyle", "std", "-warn", "off",
    "-i", "src", "-i", "src/MSL", "-i", "src/Runtime",
    "-i", "extern/dolphin/include", "-i", "src/melee",
    "-i", "src/melee/ft/chara", "-i", "src/sysdolphin",
    "-lang=c", "-c"
]

def has_local_compiler() -> bool:
    """Check if local mwcc compiler is available."""
    return WIBO.exists() and SJISWRAP.exists() and MWCC.exists()


def get_objdump_cmd() -> Path:
    """Get the best available objdump."""
    if OBJDUMP.exists():
        return OBJDUMP
    if OBJDUMP_GNU.exists():
        return OBJDUMP_GNU
    return Path("powerpc-linux-gnu-objdump")


def load_units() -> dict:
    """Load unit configuration from objdiff.json."""
    if not OBJDIFF_JSON.exists():
        return {}
    with open(OBJDIFF_JSON) as f:
        data = json.load(f)
    return {u["name"]: u for u in data.get("units", [])}


def get_target_asm_from_s_file(unit: dict, func_name: str) -> str | None:
    """Extract target assembly from the .s file.

    Returns assembly instructions only, keeping symbol references intact.
    Symbol refs like @ha, @l, @sda21 are preserved.
    """
    # Find the .s file path from the target_path
    target_path = MELEE_REPO / unit.get("target_path", "")
    if not target_path.exists():
        return None

    # The .s file is in asm/ directory with same relative structure
    # target_path: build/GALE01/obj/melee/vi/vi0102.o
    # s_file: build/GALE01/asm/melee/vi/vi0102.s
    target_rel = target_path.relative_to(MELEE_REPO / "build/GALE01")
    s_path = MELEE_REPO / "build/GALE01/asm" / str(target_rel).replace("obj/", "").replace(".o", ".s")

    if not s_path.exists():
        return None

    # Parse the .s file to extract the function
    content = s_path.read_text()

    # Find function start: .fn func_name, global
    func_pattern = rf'^\.fn\s+{re.escape(func_name)}\s*,\s*\w+'
    func_match = re.search(func_pattern, content, re.MULTILINE)
    if not func_match:
        return None

    start = func_match.end()

    # Find function end (next .fn or .endfn or end of file)
    end_match = re.search(r'^\.(?:fn|endfn)\b', content[start:], re.MULTILINE)
    if end_match:
        func_content = content[start:start + end_match.start()]
    else:
        func_content = content[start:]

    # Extract assembly instructions - keep symbol references intact
    asm_lines = []
    for line in func_content.split('\n'):
        line = line.strip()
        # Skip empty lines
        if not line:
            continue

        # Keep labels
        if line.startswith('.L_'):
            asm_lines.append(line)
            continue

        # Skip other directives
        if line.startswith('.'):
            continue

        # Match instruction lines: /* ADDR OFFSET */ instruction
        match = re.match(r'/\*.*\*/\s+(.+)', line)
        if match:
            insn = match.group(1).strip()
            asm_lines.append(insn)

    return '\n'.join(asm_lines)


def disassemble_function(obj_path: Path, func_name: str) -> str | None:
    """Disassemble a function from an object file using objdump."""
    if not obj_path.exists():
        return None

    result = subprocess.run(
        [str(OBJDUMP), "-d", str(obj_path)],
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        return None

    # Extract function
    lines = result.stdout.split("\n")
    in_func = False
    func_lines = []

    for line in lines:
        if f"<{func_name}>:" in line:
            in_func = True
            func_lines = [line]
            continue
        if in_func:
            if not line.strip() or ("<" in line and ">:" in line and func_name not in line):
                break
            func_lines.append(line)

    return "\n".join(func_lines) if func_lines else None


def extract_local_types(content: str, func_code: str) -> str:
    """Extract local typedef and struct definitions referenced by a function.

    Looks for type names in the function that match local definitions in the file.
    Returns the type definitions as a string to prepend to the function.
    """
    # Find all identifiers in the function (potential type names)
    func_identifiers = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', func_code))

    type_defs = []

    # Match typedef struct { ... } TypeName; (anonymous struct typedef)
    typedef_struct_pattern = r'^typedef\s+struct\s*\{[^}]*\}\s*(\w+)\s*;'
    for match in re.finditer(typedef_struct_pattern, content, re.MULTILINE | re.DOTALL):
        type_name = match.group(1)
        if type_name in func_identifiers:
            type_defs.append(match.group(0))

    # Match typedef struct Name { ... } TypeName; (named struct typedef)
    typedef_named_struct_pattern = r'^typedef\s+struct\s+\w+\s*\{[^}]*\}\s*(\w+)\s*;'
    for match in re.finditer(typedef_named_struct_pattern, content, re.MULTILINE | re.DOTALL):
        type_name = match.group(1)
        if type_name in func_identifiers and match.group(0) not in type_defs:
            type_defs.append(match.group(0))

    # Match simple typedefs: typedef OldType NewType;
    simple_typedef_pattern = r'^typedef\s+(?:struct\s+)?\w+[\s\*]*(\w+)\s*;'
    for match in re.finditer(simple_typedef_pattern, content, re.MULTILINE):
        type_name = match.group(1)
        if type_name in func_identifiers and match.group(0) not in type_defs:
            type_defs.append(match.group(0))

    # Match standalone struct definitions: struct Name { ... };
    struct_def_pattern = r'^struct\s+(\w+)\s*\{[^}]*\}\s*;'
    for match in re.finditer(struct_def_pattern, content, re.MULTILINE | re.DOTALL):
        struct_name = match.group(1)
        if struct_name in func_identifiers and match.group(0) not in type_defs:
            type_defs.append(match.group(0))

    # Match union typedefs with nested structs (common pattern in Melee)
    # } TypeName; at end of a typedef block
    union_typedef_pattern = r'^typedef\s+(?:union|struct)\s*(?:\w+\s*)?\{[\s\S]*?\}\s*(\w+)\s*;'
    for match in re.finditer(union_typedef_pattern, content, re.MULTILINE):
        type_name = match.group(1)
        if type_name in func_identifiers and match.group(0) not in type_defs:
            type_defs.append(match.group(0))

    return '\n'.join(type_defs)


def extract_static_definitions(content: str, func_code: str) -> str:
    """Extract static variable definitions referenced by a function.

    Looks for identifiers in the function that match static definitions in the file.
    Returns the static definitions as a string to prepend to the function.
    """
    # Find all identifiers in the function (words that could be variable names)
    func_identifiers = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', func_code))

    # Find static struct/variable definitions in the file
    # Pattern matches: static struct { ... } name; or static TYPE name; or static TYPE name = ...;
    static_defs = []

    # Match static struct definitions with initializers or without
    struct_pattern = r'^static\s+struct\s*(?:\w+\s*)?\{[^}]*\}\s*(\w+)\s*(?:=\s*\{[^;]*\})?;'
    for match in re.finditer(struct_pattern, content, re.MULTILINE | re.DOTALL):
        var_name = match.group(1)
        if var_name in func_identifiers:
            static_defs.append(match.group(0))

    # Match simpler static definitions (static TYPE name;)
    simple_pattern = r'^static\s+(?:const\s+)?(?:struct\s+\w+\s*\*?|\w+)\s+(\w+)\s*(?:\[[^\]]*\])?\s*(?:=\s*[^;]+)?;'
    for match in re.finditer(simple_pattern, content, re.MULTILINE):
        var_name = match.group(1)
        if var_name in func_identifiers and match.group(0) not in static_defs:
            static_defs.append(match.group(0))

    return '\n'.join(static_defs)


def extract_inline_functions(content: str, func_code: str) -> str:
    """Extract static inline functions referenced by the main function.

    Recursively extracts inline functions and their dependencies (types, other inlines).
    Returns the inline function definitions as a string to prepend.
    """
    # Find all identifiers in the function
    func_identifiers = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', func_code))

    inline_defs = []
    processed = set()

    def find_inline_function(name: str) -> str | None:
        """Find and extract a static inline function by name."""
        if name in processed:
            return None
        processed.add(name)

        # Match static inline function: static inline TYPE name(args) { ... }
        pattern = rf'^static\s+inline\s+[\w\s\*]+\s+{re.escape(name)}\s*\([^)]*\)\s*\{{'
        match = re.search(pattern, content, re.MULTILINE)
        if not match:
            return None

        start = match.start()

        # Find matching closing brace
        brace_count = 0
        in_func = False
        end = start

        for i, char in enumerate(content[start:], start):
            if char == '{':
                brace_count += 1
                in_func = True
            elif char == '}':
                brace_count -= 1
                if in_func and brace_count == 0:
                    end = i + 1
                    break

        return content[start:end]

    # Find all inline functions referenced by the main function
    to_check = list(func_identifiers)
    while to_check:
        name = to_check.pop(0)
        inline_code = find_inline_function(name)
        if inline_code and inline_code not in inline_defs:
            inline_defs.append(inline_code)
            # Check for nested inline references
            nested_ids = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', inline_code))
            for nested in nested_ids:
                if nested not in processed:
                    to_check.append(nested)

    return '\n\n'.join(inline_defs)


def extract_macros(content: str, func_code: str) -> str:
    """Extract #define macros referenced by the function.

    Looks for macro names used in the function code that have local #define definitions.
    Only extracts simple value macros (not function-like macros with complex bodies).
    """
    # Find all identifiers in the function (potential macro names)
    func_identifiers = set(re.findall(r'\b([A-Z_][A-Z0-9_]*)\b', func_code))

    macro_defs = []

    # Match #define MACRO_NAME value (simple value macros)
    # Also handles multiline macros with backslash continuation
    for identifier in func_identifiers:
        pattern = rf'^#define\s+{re.escape(identifier)}\s+[^\n\\]+(?:\\\n[^\n\\]+)*'
        match = re.search(pattern, content, re.MULTILINE)
        if match:
            macro_defs.append(match.group(0))

    return '\n'.join(macro_defs)


def extract_function_from_source(source_path: Path, func_name: str) -> str | None:
    """Extract a function's source code from a C file.

    Also extracts:
    - Local typedef and struct definitions that the function references
    - Static variable definitions that the function references
    - Static inline functions referenced by the function
    - #define macros used by the function
    """
    if not source_path.exists():
        return None

    content = source_path.read_text()

    # Find function definition (expanded to include 'bool' return type)
    pattern = rf'^[ \t]*((?:static|inline|extern|const|unsigned|signed|long|short|void|int|char|float|double|bool|struct\s+\w+|u8|u16|u32|s8|s16|s32|f32|M2C_UNK|UNK_RET)[\w\s\*]*)\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{'
    match = re.search(pattern, content, re.MULTILINE)
    if not match:
        # Fallback to simpler pattern
        pattern = rf'(void|int|long|bool|s32|u32|M2C_UNK|UNK_RET)\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{'
        match = re.search(pattern, content)
        if not match:
            return None

    start = match.start()

    # Find matching closing brace
    brace_count = 0
    in_func = False
    end = start

    for i, char in enumerate(content[start:], start):
        if char == '{':
            brace_count += 1
            in_func = True
        elif char == '}':
            brace_count -= 1
            if in_func and brace_count == 0:
                end = i + 1
                break

    func_code = content[start:end]

    # Extract inline functions referenced by the function
    inline_funcs = extract_inline_functions(content, func_code)

    # Build combined code for type extraction (main func + inlines)
    all_code = func_code
    if inline_funcs:
        all_code = inline_funcs + '\n\n' + func_code

    # Extract local type definitions referenced by the function and inlines
    type_defs = extract_local_types(content, all_code)

    # Extract static definitions referenced by the function and inlines
    static_defs = extract_static_definitions(content, all_code)

    # Extract macros referenced by the function and inlines
    macros = extract_macros(content, all_code)

    # Combine all definitions: macros first, types, statics, inlines, then main function
    prefix_parts = []
    if macros:
        prefix_parts.append(macros)
    if type_defs:
        prefix_parts.append(type_defs)
    if static_defs:
        prefix_parts.append(static_defs)
    if inline_funcs:
        prefix_parts.append(inline_funcs)

    if prefix_parts:
        return '\n\n'.join(prefix_parts) + '\n\n' + func_code
    return func_code


def compile_local(source_code: str, output_path: Path, func_name: str = None) -> tuple[bool, str]:
    """Compile source code using local mwcc compiler.

    Returns (success, error_message).
    If func_name is provided, filters out its declaration from context to avoid conflicts.
    """
    if not has_local_compiler():
        return False, "Local compiler not available"

    if not CONTEXT_TXT.exists():
        return False, f"Context file not found: {CONTEXT_TXT}"

    # Read context and filter out conflicting function declaration
    context = CONTEXT_TXT.read_text()
    if func_name:
        # Remove the function's forward declaration from context
        # Pattern matches: /* ADDR */ RETURN_TYPE func_name(args);
        pattern = rf'^/\*[^*]*\*/\s+\S+\s+{re.escape(func_name)}\s*\([^)]*\)\s*;.*$'
        context = re.sub(pattern, f'/* {func_name} declaration removed - defined below */', context, flags=re.MULTILINE)

    # Create temp file with context + source
    with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False) as f:
        temp_c = Path(f.name)
        # Write filtered context
        f.write(context)
        f.write("\n")
        # Write source
        f.write(source_code)

    try:
        # Compile with mwcc
        cmd = [str(WIBO), str(SJISWRAP), str(MWCC)] + MWCC_FLAGS + [str(temp_c), "-o", str(output_path)]
        result = subprocess.run(
            cmd,
            cwd=MELEE_REPO,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            return False, result.stderr or result.stdout

        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Compilation timed out"
    except Exception as e:
        return False, str(e)
    finally:
        temp_c.unlink(missing_ok=True)


def score_object_files(target_o: Path, current_o: Path, func_name: str) -> tuple[int, int, list]:
    """Compare two object files and compute a score.

    Returns (score, max_score, diff_lines).
    Lower score = better match. Score 0 = perfect match.
    """
    objdump = get_objdump_cmd()

    # Disassemble both files
    def get_asm(obj_path: Path) -> list[str]:
        result = subprocess.run(
            [str(objdump), "-d", str(obj_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return []

        lines = []
        in_func = False
        for line in result.stdout.split("\n"):
            if f"<{func_name}>:" in line:
                in_func = True
                continue
            if in_func:
                if not line.strip():
                    continue
                if "<" in line and ">:" in line and func_name not in line:
                    break
                # Extract instruction mnemonic and operands
                match = re.match(r'^\s*[0-9a-f]+:\s+[0-9a-f ]+\s+(.+)', line, re.I)
                if match:
                    insn = match.group(1).strip()
                    # Normalize: remove symbol addresses and relocatable offsets
                    insn = re.sub(r'\b0x[0-9a-f]+\b', 'ADDR', insn, flags=re.I)
                    insn = re.sub(r'\b[0-9a-f]{8}\b', 'ADDR', insn)
                    # Normalize memory offsets like "1736(r5)" to "OFFSET(r5)"
                    # These differ due to relocations but instruction structure is same
                    insn = re.sub(r'-?\d+\((r\d+)\)', r'OFFSET(\1)', insn)
                    # Normalize immediate values in address-loading instructions
                    # These differ due to relocations (string literals, data refs)
                    # e.g., "addi r3,r31,10952" vs "addi r3,r31,0"
                    insn = re.sub(r'^(addi|addis|lis|ori|oris)\s+(r\d+),(r\d+),(-?\d+)$', r'\1 \2,\3,IMM', insn)
                    insn = re.sub(r'^(lis)\s+(r\d+),(-?\d+)$', r'\1 \2,IMM', insn)
                    # Normalize branch target addresses
                    # e.g., "bl 3a0 <func+0x3c>" vs "bl 3c <func+0x3c>"
                    # The raw address differs but the instruction is the same
                    insn = re.sub(r'^(b\w*)\s+[0-9a-f]+\s+(<.+>)$', r'\1 \2', insn, flags=re.I)
                    lines.append(insn)
        return lines

    target_asm = get_asm(target_o)
    current_asm = get_asm(current_o)

    if not target_asm:
        return 999999, 1, ["Could not disassemble target"]

    max_score = len(target_asm) * 100  # Each instruction worth 100 points max

    # Simple diff scoring (similar to permuter)
    # Penalties: same=0, regalloc=5, reorder=60, insert/delete=100
    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None, current_asm, target_asm)

    score = 0
    diff_lines = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                cur = current_asm[i1 + k]
                tgt = target_asm[j1 + k]
                if cur != tgt:
                    # Same mnemonic but different operands (regalloc)
                    score += 5
                    diff_lines.append(f"  ~ {tgt} -> {cur}")
        elif tag == 'replace':
            for k in range(max(i2 - i1, j2 - j1)):
                if k < (j2 - j1):
                    diff_lines.append(f"  - {target_asm[j1 + k]}")
                if k < (i2 - i1):
                    diff_lines.append(f"  + {current_asm[i1 + k]}")
            score += max(i2 - i1, j2 - j1) * 100
        elif tag == 'delete':
            for k in range(i1, i2):
                diff_lines.append(f"  + {current_asm[k]}")
            score += (i2 - i1) * 100
        elif tag == 'insert':
            for k in range(j1, j2):
                diff_lines.append(f"  - {target_asm[k]}")
            score += (j2 - j1) * 100

    return score, max_score, diff_lines


def cmd_scratch_local(func_name: str, func_unit: dict, func_unit_name: str, func_code: str) -> dict:
    """Test a function using local mwcc compiler (fast)."""
    # Get target object file
    target_path = MELEE_REPO / func_unit.get("target_path", "")
    if not target_path.exists():
        print(f"Target object not found: {target_path}", file=sys.stderr)
        sys.exit(1)

    # Compile current code
    with tempfile.NamedTemporaryFile(suffix='.o', delete=False) as f:
        current_o = Path(f.name)

    try:
        success, error = compile_local(func_code, current_o, func_name)

        if not success:
            print(f"Compilation failed: {error}", file=sys.stderr)
            sys.exit(1)

        # Score the result
        score, max_score, diff_lines = score_object_files(target_path, current_o, func_name)

        if max_score > 0:
            match_pct = (1 - score / max_score) * 100
        else:
            match_pct = 100.0 if score == 0 else 0.0

        print(f"Function: {func_name}")
        print(f"Unit: {func_unit_name}")
        print(f"Mode: Local mwcc (quick check - NOT authoritative)")
        print()
        print(f"Match: {match_pct:.2f}% (score: {score}/{max_score})")
        print()

        if match_pct == 100:
            print("SCRATCH MATCH. This is NOT authoritative. Run ninja + verify to confirm.")
        elif match_pct >= 99:
            print("Very close! Check register allocation or minor differences.")
            if diff_lines:
                print("\nDifferences:")
                for line in diff_lines[:20]:
                    print(line)
        elif match_pct >= 90:
            print("Good progress. Review the diff for remaining issues.")
            print("LOW MATCH. Fix control flow and expression structure before trying permuter.")
        else:
            print("LOW MATCH. Fix control flow and expression structure before trying permuter.")

        if diff_lines and match_pct < 99:
            print("\nDifferences:")
            for line in diff_lines[:30]:
                print(line)

        print()
        print("NOTE: scratch compiles in isolation and may show LOWER % than actual.")
        print("      Run 'ninja' + 'verify' for authoritative match percentage.")

        return {"score": score, "max_score": max_score, "match_pct": match_pct}

    finally:
        current_o.unlink(missing_ok=True)


def cmd_scratch(func_name: str):
    """Test a function against target binary using local mwcc compiler."""
    if not has_local_compiler():
        print("Local compiler not available.", file=sys.stderr)
        print("Required: build/tools/wibo, build/tools/sjiswrap.exe, build/compilers/GC/1.2.5n/mwcceppc.exe", file=sys.stderr)
        sys.exit(1)

    units = load_units()

    # Check context.txt exists
    if not CONTEXT_TXT.exists():
        print(f"Error: context.txt not found at {CONTEXT_TXT}", file=sys.stderr)
        sys.exit(1)

    # Find the function's unit
    func_unit = None
    func_unit_name = None

    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        asm = disassemble_function(target_path, func_name)
        if asm:
            func_unit = unit
            func_unit_name = unit_name
            break

    if not func_unit:
        print(f"Function {func_name} not found in any unit", file=sys.stderr)
        sys.exit(1)

    # Get source path and extract function
    source_path = MELEE_REPO / func_unit.get("metadata", {}).get("source_path", "")
    if not source_path.exists():
        print(f"Source file not found: {source_path}", file=sys.stderr)
        sys.exit(1)

    func_code = extract_function_from_source(source_path, func_name)
    if not func_code:
        print(f"Function {func_name} not found in source file", file=sys.stderr)
        print("(Might be stubbed with /// #function_name marker)", file=sys.stderr)
        sys.exit(1)

    return cmd_scratch_local(func_name, func_unit, func_unit_name, func_code)


def cmd_asm(func_name: str):
    """Get target assembly for a function (objdump format)."""
    units = load_units()

    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        asm = disassemble_function(target_path, func_name)
        if asm:
            print(f"# Unit: {unit_name}")
            print(f"# Source: {unit.get('metadata', {}).get('source_path', 'unknown')}")
            print()
            print(asm)
            return

    print(f"Function {func_name} not found in any unit", file=sys.stderr)
    sys.exit(1)


def cmd_list(limit: int = 50):
    """List incomplete units."""
    units = load_units()

    incomplete = []
    for unit_name, unit in units.items():
        metadata = unit.get("metadata", {})
        if metadata.get("complete", False):
            continue

        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        # Get function count
        result = subprocess.run(
            [str(OBJDUMP), "-t", str(target_path)],
            capture_output=True, text=True, timeout=10
        )

        func_count = sum(1 for line in result.stdout.split("\n") if " F " in line)
        source = metadata.get("source_path", "unknown")

        incomplete.append((unit_name, source, func_count))

    incomplete.sort(key=lambda x: x[2])  # Sort by function count

    print(f"Incomplete units ({len(incomplete)} total, showing first {limit}):\n")
    for unit_name, source, func_count in incomplete[:limit]:
        print(f"  {func_count:3d} funcs | {unit_name}")
        print(f"           {source}")
        print()


def cmd_funcs(unit_name: str):
    """List functions in a unit."""
    units = load_units()

    if unit_name not in units:
        matches = [u for u in units if unit_name in u]
        if len(matches) == 1:
            unit_name = matches[0]
        else:
            print(f"Unit not found: {unit_name}", file=sys.stderr)
            sys.exit(1)

    unit = units[unit_name]
    target_path = MELEE_REPO / unit.get("target_path", "")

    result = subprocess.run(
        [str(OBJDUMP), "-t", str(target_path)],
        capture_output=True, text=True
    )

    print(f"Functions in {unit_name}:\n")
    for line in result.stdout.split("\n"):
        if " F " in line:
            parts = line.split()
            if len(parts) >= 6:
                print(f"  {parts[-1]}")


def cmd_source(unit_name: str):
    """Show the source file path for a unit."""
    units = load_units()

    if unit_name not in units:
        matches = [u for u in units if unit_name in u]
        if len(matches) == 1:
            unit_name = matches[0]
        else:
            print(f"Unit not found: {unit_name}", file=sys.stderr)
            sys.exit(1)

    unit = units[unit_name]
    source_path = unit.get("metadata", {}).get("source_path")
    if source_path:
        full_path = MELEE_REPO / source_path
        print(full_path)
    else:
        print(f"No source_path for {unit_name}", file=sys.stderr)
        sys.exit(1)


def cmd_verify(func_name: str):
    """Verify function match against official report.json (AUTHORITATIVE)."""
    report_path = MELEE_REPO / "build/GALE01/report.json"

    if not report_path.exists():
        print("ERROR: report.json not found. Run 'ninja' in melee/ first.", file=sys.stderr)
        print()
        print("  cd ~/melee-decomp-agent/melee && ninja")
        sys.exit(1)

    with open(report_path) as f:
        report = json.load(f)

    found = False
    for unit in report.get("units", []):
        for func in unit.get("functions", []):
            fname = func.get("name", "")
            if func_name.lower() in fname.lower():
                found = True
                pct = func.get("fuzzy_match_percent")
                unit_name = unit.get("name", "unknown")

                if pct == 100.0:
                    print(f"✓ {fname}: 100% MATCHED")
                    print(f"  Unit: {unit_name}")
                elif pct is None:
                    print(f"? {fname}: Not decompiled yet (still in .s file)")
                    print(f"  Unit: {unit_name}")
                else:
                    print(f"✗ {fname}: {pct}%")
                    print(f"  Unit: {unit_name}")
                print()

    if not found:
        print(f"Function '{func_name}' not found in report.json")
        print("Check the function name or run 'ninja' to regenerate the report.")
        sys.exit(1)


def is_function_implemented(source_path: Path, func_name: str, all_funcs_in_unit: list[str]) -> bool:
    """Check if a function is implemented (not stubbed) in the source file."""
    if not source_path.exists():
        return False

    try:
        content = source_path.read_text()

        # Check if function definition exists
        if re.search(rf'\b{re.escape(func_name)}\s*\([^)]*\)\s*\{{', content):
            return True

        # Check for stub marker
        if re.search(rf'///\s*#{re.escape(func_name)}\b', content):
            return False
        if re.search(rf'/\*\s*#{re.escape(func_name)}\s*\*/', content):
            return False

        if func_name not in content:
            return False

        return True
    except Exception:
        return False


def get_objdiff_hash() -> str:
    """Get hash of objdiff.json for cache invalidation."""
    if not OBJDIFF_JSON.exists():
        return ""
    with open(OBJDIFF_JSON, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def cmd_permute(func_name: str, iterations: int = 0):
    """Set up and run the permuter for a function.

    Creates the permuter directory with all required files,
    then runs the permuter to search for improvements.

    Uses the build-generated .ctx file (from decompctx.py) which matches
    exactly what mwcc sees during the actual build, ensuring identical codegen.
    """
    import shutil

    PERMUTER_DIR = Path.home() / "decomp-permuter"
    if not PERMUTER_DIR.exists():
        print(f"Permuter not found at {PERMUTER_DIR}", file=sys.stderr)
        print("Clone it with: git clone https://github.com/simonlindholm/decomp-permuter.git ~/decomp-permuter")
        sys.exit(1)

    units = load_units()

    # Find the function's unit
    func_unit = None
    func_unit_name = None

    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        asm = disassemble_function(target_path, func_name)
        if asm:
            func_unit = unit
            func_unit_name = unit_name
            break

    if not func_unit:
        print(f"Function {func_name} not found in any unit", file=sys.stderr)
        sys.exit(1)

    # Get source path (relative to MELEE_REPO)
    source_path_rel = func_unit.get("metadata", {}).get("source_path", "")
    source_path = MELEE_REPO / source_path_rel

    if not source_path.exists():
        print(f"Source file not found: {source_path}", file=sys.stderr)
        sys.exit(1)

    # We use the actual source file (not ctx) for permuting because:
    # 1. ninja compiles the source file, not the ctx file
    # 2. The ctx file has auto-generated forward declarations that can cause conflicts
    # 3. Using the source file ensures exact parity with the ninja build

    func_code = extract_function_from_source(source_path, func_name)
    if not func_code:
        print(f"Function {func_name} not found in source file", file=sys.stderr)
        sys.exit(1)

    # Get target object path
    target_o = MELEE_REPO / func_unit.get("target_path", "")

    # Create permuter directory
    perm_dir = PERMUTER_DIR / "nonmatchings" / func_name
    perm_dir.mkdir(parents=True, exist_ok=True)

    # Extract types and functions used in the code for pycparser stubs
    type_pattern = re.compile(r'\b(HSD_\w+|[A-Z][a-z]+[A-Z]\w*|\w+Desc)\b')
    func_pattern = re.compile(r'\b(\w+)\s*\(')
    # Global variables often have address suffixes like _804D7849 or lbl_8046DBE8
    global_pattern = re.compile(r'\b((?:lbl_|HSD_\w*_)[0-9A-Fa-f]{8})\b')

    types_used = set(type_pattern.findall(func_code))
    funcs_called = set(func_pattern.findall(func_code)) - {func_name, 'if', 'for', 'while', 'switch'}
    globals_used = set(global_pattern.findall(func_code)) - funcs_called  # Exclude function calls

    # Remove function names and global names from types to avoid conflicts
    types_used = types_used - funcs_called - globals_used

    # Build base.c with stubs for pycparser
    # Start with standard type definitions that pycparser needs
    standard_types = {
        's8', 'u8', 's16', 'u16', 's32', 'u32', 's64', 'u64',
        'f32', 'f64', 'bool', 'size_t', 'M2C_UNK',
        'Vec2', 'Vec3', 'Mtx', 'Quaternion', 'GXColor',
    }
    base_c_lines = [
        "/* Standard type stubs for pycparser */",
        "typedef int s8;",
        "typedef int u8;",
        "typedef int s16;",
        "typedef int u16;",
        "typedef int s32;",
        "typedef int u32;",
        "typedef int s64;",
        "typedef int u64;",
        "typedef int f32;",
        "typedef int f64;",
        "typedef int bool;",
        "typedef int size_t;",
        "typedef int M2C_UNK;",
        "typedef int Vec2;",
        "typedef int Vec3;",
        "typedef int Mtx;",
        "typedef int Quaternion;",
        "typedef int GXColor;",
        "#define true 1",
        "#define false 0",
        "#define NULL ((void*)0)",
        "",
        "/* Additional type stubs for pycparser */",
    ]
    for t in sorted(types_used):
        # Skip types we already defined or that are actually functions
        if t not in standard_types:
            base_c_lines.append(f"typedef int {t};")

    base_c_lines.append("")
    base_c_lines.append("/* Function stubs for pycparser */")
    for f in sorted(funcs_called):
        base_c_lines.append(f"void {f}();")

    base_c_lines.append("")
    base_c_lines.append("/* Global variable stubs for pycparser */")
    for g in sorted(globals_used):
        base_c_lines.append(f"extern int {g};")

    base_c_lines.append("")

    # Add the function code
    func_lines = func_code.split('\n')
    base_c_lines.extend(func_lines)

    base_c = '\n'.join(base_c_lines)
    (perm_dir / "base.c").write_text(base_c)

    # Read and process the ACTUAL SOURCE FILE (not ctx) for accurate compilation
    # This ensures we match exactly what ninja compiles
    source_content = source_path.read_text()

    # Find and replace the target function with a placeholder
    def find_function_bounds(content: str, fname: str) -> tuple[int, int] | None:
        """Find the start and end positions of a function implementation."""
        # Match function signature - handles various return types and modifiers
        sig_pattern = re.compile(
            rf'^[^\S\n]*'  # Optional leading whitespace (not newline)
            rf'(?:static\s+|inline\s+|extern\s+)*'  # Optional modifiers
            rf'[\w\s\*]+?'  # Return type
            rf'\b{re.escape(fname)}\s*'  # Function name
            rf'\([^)]*\)\s*'  # Parameters
            rf'\{{',  # Opening brace
            re.MULTILINE
        )

        match = sig_pattern.search(content)
        if not match:
            return None

        start = match.start()
        # Find matching closing brace
        brace_count = 1
        pos = match.end()
        while pos < len(content) and brace_count > 0:
            if content[pos] == '{':
                brace_count += 1
            elif content[pos] == '}':
                brace_count -= 1
            pos += 1

        if brace_count == 0:
            return (start, pos)
        return None

    func_bounds = find_function_bounds(source_content, func_name)
    if func_bounds:
        start, end = func_bounds
        source_content = (
            source_content[:start] +
            f"\n/* PERMUTER_FUNCTION_PLACEHOLDER: {func_name} */\n" +
            source_content[end:]
        )
    else:
        print(f"Warning: Could not find {func_name} implementation in source, appending placeholder")
        source_content += f"\n/* PERMUTER_FUNCTION_PLACEHOLDER: {func_name} */\n"

    (perm_dir / "source.c").write_text(source_content)

    # Create target.o containing ONLY the target function
    # The permuter compares entire objects, so we need to extract just our function
    # We use objcopy to create a minimal object with only the target function bytes
    target_func_o = perm_dir / "target.o"
    shutil.copy(target_o, target_func_o)

    # Get function info (offset and size) from the ROM object
    result = subprocess.run(
        ["powerpc-linux-gnu-objdump", "-t", str(target_o)],
        capture_output=True, text=True
    )
    func_info = None
    for line in result.stdout.splitlines():
        if f" F .text" in line and func_name in line:
            parts = line.split()
            # Format: OFFSET FLAGS SECTION SIZE NAME
            func_info = {"offset": int(parts[0], 16), "size": int(parts[4], 16)}
            break

    if func_info:
        # Create an assembly file with just the target function's bytes
        # Extract function bytes from the ROM object
        result = subprocess.run(
            ["powerpc-linux-gnu-objcopy", "-O", "binary", "--only-section=.text",
             str(target_o), "/tmp/target_text.bin"],
            capture_output=True
        )
        if result.returncode == 0:
            # Read just the function bytes
            with open("/tmp/target_text.bin", "rb") as f:
                f.seek(func_info["offset"])
                func_bytes = f.read(func_info["size"])

            # Create assembly file with the function
            asm_content = f'''.section .text
.global {func_name}
.type {func_name}, @function
{func_name}:
'''
            # Add bytes as .byte directives (4 bytes per line for readability)
            for i in range(0, len(func_bytes), 4):
                chunk = func_bytes[i:i+4]
                asm_content += "    .byte " + ", ".join(f"0x{b:02x}" for b in chunk) + "\n"

            asm_content += f".size {func_name}, . - {func_name}\n"

            asm_file = perm_dir / "target.s"
            asm_file.write_text(asm_content)

            # Assemble to create target.o with just the function
            subprocess.run(
                ["powerpc-linux-gnu-as", "-o", str(target_func_o), str(asm_file)],
                capture_output=True
            )

    # Create compile.sh that uses local mwcc
    # Uses the ACTUAL SOURCE FILE (not ctx) to match ninja exactly
    compile_sh = f'''#!/bin/bash
# Compile script for permuter - compiles actual source file for exact match with ninja
# The source.c contains the actual source with target function replaced by placeholder.
# This script extracts the function from base.c and substitutes it into the placeholder.
set -e

MELEE_DIR="{MELEE_REPO}"
SCRIPT_DIR="${{0%/*}}"
SOURCE="$SCRIPT_DIR/source.c"

# Parse args - convert to absolute paths before cd
INPUT="$(realpath "$1")"
[[ "$2" == "-o" ]] && OUTPUT="$(realpath -m "$3")" || OUTPUT="$(realpath -m "$2")"

# Put temp file in same directory as original source for correct #include resolution
TEMP_C="$MELEE_DIR/{source_path_rel.rsplit('/', 1)[0]}/permuter_temp_$$.c"

# Extract just the function code from INPUT (skip pycparser stubs) to a temp file
# The function starts at first line matching "type func_name(" followed by "{{"
FUNC_FILE="/dev/shm/permuter_func_$$.c"
awk '
    /^[a-zA-Z_].*\\(.*\\)[[:space:]]*$/{{
        if (getline nl > 0 && nl ~ /^{{/) {{
            print
            print nl
            found = 1
            next
        }}
    }}
    found {{ print }}
' "$INPUT" > "$FUNC_FILE"

# Read source and replace the placeholder with the function code
# Use getline to read func file - avoids awk -v interpreting escape sequences like \\n
awk -v func_file="$FUNC_FILE" '
    /PERMUTER_FUNCTION_PLACEHOLDER/ {{
        while ((getline line < func_file) > 0) print line
        close(func_file)
        next
    }}
    {{ print }}
' "$SOURCE" > "$TEMP_C"
rm -f "$FUNC_FILE"

cd "$MELEE_DIR"
TEMP_O="/dev/shm/permuter_full_$$.o"

"$MELEE_DIR/build/tools/wibo" "$MELEE_DIR/build/tools/sjiswrap.exe" \\
    "$MELEE_DIR/build/compilers/GC/1.2.5n/mwcceppc.exe" \\
    -nowraplines -cwd source -Cpp_exceptions off -proc gekko \\
    -fp hardware -align powerpc -nosyspath -fp_contract on \\
    -O4,p -multibyte -enum int -nodefaults -inline auto \\
    -pragma 'cats off' -pragma 'warn_notinlined off' \\
    -RTTI off -str reuse -DBUILD_VERSION=0 -DVERSION_GALE01 \\
    -DNDEBUG=1 -maxerrors 1 -msgstyle std -warn off \\
    -i src -i src/MSL -i src/Runtime -i extern/dolphin/include \\
    -i src/melee -i src/melee/ft/chara -i src/sysdolphin \\
    -lang=c -c "$TEMP_C" -o "$TEMP_O"

# Extract only the target function to match target.o structure
# Get function offset and size
FUNC_INFO=$(powerpc-linux-gnu-objdump -t "$TEMP_O" | grep " F .text.*{func_name}$" | head -1)
if [ -n "$FUNC_INFO" ]; then
    FUNC_OFFSET=$((16#$(echo "$FUNC_INFO" | awk '{{print $1}}')))
    FUNC_SIZE=$((16#$(echo "$FUNC_INFO" | awk '{{print $5}}')))

    # Extract .text section
    powerpc-linux-gnu-objcopy -O binary --only-section=.text "$TEMP_O" /dev/shm/text_$$.bin

    # Extract just the function bytes
    dd if=/dev/shm/text_$$.bin of=/dev/shm/func_$$.bin bs=1 skip=$FUNC_OFFSET count=$FUNC_SIZE 2>/dev/null

    # Create assembly with just the function
    cat > /dev/shm/func_$$.s << ASM_EOF
.section .text
.global {func_name}
.type {func_name}, @function
{func_name}:
ASM_EOF
    # Convert binary to .byte directives (4 bytes per line)
    od -An -tx1 /dev/shm/func_$$.bin | sed 's/^ *//' | while read -r line; do
        if [ -n "$line" ]; then
            echo "    .byte $(echo $line | sed 's/ /, 0x/g; s/^/0x/')"
        fi
    done >> /dev/shm/func_$$.s
    echo ".size {func_name}, . - {func_name}" >> /dev/shm/func_$$.s

    # Assemble to final output
    powerpc-linux-gnu-as -o "$OUTPUT" /dev/shm/func_$$.s

    rm -f /dev/shm/text_$$.bin /dev/shm/func_$$.bin /dev/shm/func_$$.s "$TEMP_O"
else
    # Fallback: just use the full object
    mv "$TEMP_O" "$OUTPUT"
fi
RC=$?

rm -f "$TEMP_C"
exit $RC
'''
    (perm_dir / "compile.sh").write_text(compile_sh)
    (perm_dir / "compile.sh").chmod(0o755)

    print(f"Permuter directory created: {perm_dir}")
    print()
    print("Files created:")
    print(f"  - base.c (your code with pycparser stubs)")
    print(f"  - source.c (actual source from {source_path.name} with function placeholder)")
    print(f"  - target.o (target binary)")
    print(f"  - compile.sh (local mwcc compiler)")
    print()

    if iterations > 0:
        print(f"Running permuter for {iterations} iterations...")
        print()
        result = subprocess.run(
            ["python3", "permuter.py", f"nonmatchings/{func_name}", "-j4", f"--iterations={iterations}"],
            cwd=PERMUTER_DIR,
            timeout=iterations * 2 + 60
        )
        return result.returncode
    else:
        print("To run the permuter:")
        print(f"  cd {PERMUTER_DIR}")
        print(f"  python3 permuter.py nonmatchings/{func_name} -j4")
        print()
        print("NOTE: You may need to edit base.c to fix pycparser errors:")
        print("  - Add missing typedef stubs")
        print("  - Add missing function prototypes")
        print("  - Add extern declarations for globals used")
        return 0


# =============================================================================
# Embedding-based similarity search
# =============================================================================

def get_voyage_api_key() -> str | None:
    """Get Voyage AI API key from environment."""
    return os.environ.get("VOYAGE_API_KEY")


def voyage_embed_batch(asm_texts: list[str], max_retries: int = 10) -> list[list[float]]:
    """Embed a batch of assembly texts using Voyage AI with retry on rate limit."""
    import time

    api_key = get_voyage_api_key()
    if not api_key:
        raise ValueError("VOYAGE_API_KEY environment variable not set")

    data = {
        "input": asm_texts,
        "model": VOYAGE_MODEL,
        "input_type": "document",
    }

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                VOYAGE_API_URL,
                data=json.dumps(data).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}',
                },
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=60) as response:
                result = json.loads(response.read().decode('utf-8'))

            # Sort by index to maintain order
            embeddings = sorted(result['data'], key=lambda x: x['index'])
            return [e['embedding'] for e in embeddings]

        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                # Rate limited - wait with exponential backoff
                wait_time = 21 * (attempt + 1)  # 21s, 42s, 63s, etc.
                print(f"    Rate limited, waiting {wait_time}s...", file=sys.stderr)
                time.sleep(wait_time)
                continue
            # Try to read error details
            try:
                error_body = e.read().decode('utf-8')
                raise Exception(f"HTTP {e.code}: {error_body[:200]}")
            except Exception:
                raise

    raise Exception(f"Failed after {max_retries} retries")


def preprocess_asm_for_embedding(asm: str) -> str | None:
    """Preprocess PowerPC assembly for embedding.

    - Strip comments
    - Remove addresses and hex bytes
    - Normalize whitespace
    - Keep instruction mnemonics and operands
    - Returns None only if empty
    """
    lines = []
    for line in asm.split('\n'):
        # Skip empty lines and labels
        line = line.strip()
        if not line or line.endswith(':'):
            continue

        # Extract instruction from objdump format: "addr: hex  insn operands"
        match = re.match(r'^\s*[0-9a-f]+:\s+[0-9a-f ]+\s+(.+)', line, re.I)
        if match:
            insn = match.group(1).strip()
            # Normalize addresses to placeholders
            insn = re.sub(r'\b0x[0-9a-f]+\b', 'ADDR', insn, flags=re.I)
            lines.append(insn)

    result = '\n'.join(lines)
    return result if result else None


def load_embeddings_cache(ignore_hash: bool = False) -> dict | None:
    """Load cached embeddings.

    Args:
        ignore_hash: If True, load cache even if objdiff.json has changed.
                     Used for sync operations that just update decompiled flags.
    """
    if not EMBEDDINGS_CACHE.exists():
        return None

    try:
        with open(EMBEDDINGS_CACHE) as f:
            cache = json.load(f)

        if not ignore_hash and cache.get("objdiff_hash") != get_objdiff_hash():
            return None

        return cache
    except (json.JSONDecodeError, KeyError):
        return None


def get_embeddings_cache_age_days() -> float | None:
    """Get the age of the embeddings cache in days, or None if no cache."""
    if not EMBEDDINGS_CACHE.exists():
        return None

    try:
        with open(EMBEDDINGS_CACHE) as f:
            cache = json.load(f)

        indexed_at = cache.get("indexed_at")
        if not indexed_at:
            return None

        from datetime import datetime
        indexed_date = datetime.fromisoformat(indexed_at)
        age = datetime.now() - indexed_date
        return age.total_seconds() / 86400  # Convert to days
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def sync_embeddings_cache() -> bool:
    """Sync the embeddings cache by updating decompiled flags.

    Returns True if sync was successful, False otherwise.
    """
    cache = load_embeddings_cache(ignore_hash=True)
    if not cache:
        return False

    units = load_units()
    updated = 0

    for func_name, data in cache["functions"].items():
        source_path = MELEE_REPO / data.get("source", "")
        if source_path.exists():
            new_status = is_function_implemented(source_path, func_name, [])
            if new_status != data["decompiled"]:
                data["decompiled"] = new_status
                updated += 1

    # Update the hash to current
    cache["objdiff_hash"] = get_objdiff_hash()
    save_embeddings_cache(cache["functions"])
    return True


def save_embeddings_cache(functions: dict):
    """Save embeddings to cache."""
    cache = {
        "objdiff_hash": get_objdiff_hash(),
        "model": VOYAGE_MODEL,
        "indexed_at": datetime.now().isoformat(),
        "functions": functions,
    }
    with open(EMBEDDINGS_CACHE, "w") as f:
        json.dump(cache, f)


def cmd_index(refresh: bool = False, sync: bool = False):
    """Generate embeddings for all functions.

    Modes:
    - Default: incremental update (only embed new/changed functions)
    - --refresh: full rebuild of all embeddings
    - --sync: just update decompiled flags without re-embedding
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    # Sync mode: just update decompiled flags without re-embedding
    if sync:
        cache = load_embeddings_cache()
        if not cache:
            print("No embeddings cache. Run 'tools.py index --refresh' first.", file=sys.stderr)
            sys.exit(1)

        units = load_units()
        updated = 0

        for func_name, data in cache["functions"].items():
            source_path = MELEE_REPO / data.get("source", "")
            if source_path.exists():
                new_status = is_function_implemented(source_path, func_name, [])
                if new_status != data["decompiled"]:
                    data["decompiled"] = new_status
                    updated += 1

        save_embeddings_cache(cache["functions"])
        decompiled_count = sum(1 for f in cache["functions"].values() if f["decompiled"])
        print(f"Synced decompiled status ({updated} changed, {decompiled_count} total decompiled)")
        return

    if not get_voyage_api_key():
        print("Error: VOYAGE_API_KEY environment variable not set", file=sys.stderr)
        print("Get an API key from https://www.voyageai.com/", file=sys.stderr)
        sys.exit(1)

    # Load existing cache for incremental mode
    existing_cache = None
    if not refresh:
        existing_cache = load_embeddings_cache(ignore_hash=True)
        if existing_cache:
            print(f"Incremental mode: {len(existing_cache['functions'])} cached functions", file=sys.stderr)

    units = load_units()

    # Phase 1: Collect all function metadata (parallel objdump -t calls)
    print("Collecting function list...", file=sys.stderr)

    # First, gather all units with valid target paths
    unit_tasks = []
    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if target_path.exists():
            source_path = MELEE_REPO / unit.get("metadata", {}).get("source_path", "")
            unit_tasks.append((unit_name, target_path, source_path))

    # Parallel function discovery from symbol tables
    func_metadata = {}  # {func_name: (unit_name, target_path, source_path)}

    def get_unit_functions(task):
        unit_name, target_path, source_path = task
        funcs = []
        try:
            result = subprocess.run(
                [str(OBJDUMP), "-t", str(target_path)],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.split("\n"):
                if " F " in line:
                    parts = line.split()
                    if len(parts) >= 6:
                        func_name = parts[-1]
                        if not func_name.startswith("gap_"):
                            funcs.append((func_name, unit_name, target_path, source_path))
        except (subprocess.TimeoutExpired, Exception):
            pass
        return funcs

    with ThreadPoolExecutor(max_workers=DISASSEMBLY_WORKERS) as executor:
        futures = [executor.submit(get_unit_functions, task) for task in unit_tasks]
        for future in as_completed(futures):
            for func_name, unit_name, target_path, source_path in future.result():
                func_metadata[func_name] = (unit_name, target_path, source_path)

    print(f"Found {len(func_metadata)} functions", file=sys.stderr)

    # Determine which functions need embedding
    functions_to_embed = []
    functions_data = {}

    for func_name, (unit_name, target_path, source_path) in func_metadata.items():
        # Check if we can reuse cached embedding
        if existing_cache and func_name in existing_cache["functions"]:
            cached = existing_cache["functions"][func_name]
            if cached.get("embedding"):
                # Reuse cached embedding, just update decompiled status
                decompiled = is_function_implemented(source_path, func_name, [])
                functions_data[func_name] = {
                    "unit": unit_name,
                    "source": str(source_path),
                    "decompiled": decompiled,
                    "embedding": cached["embedding"],
                }
                continue

        # Need to embed this function
        functions_to_embed.append((func_name, unit_name, target_path, source_path))

    print(f"Need to embed {len(functions_to_embed)} functions ({len(functions_data)} cached)", file=sys.stderr)

    if not functions_to_embed:
        # Just save with updated decompiled flags
        save_embeddings_cache(functions_data)
        decompiled_count = sum(1 for f in functions_data.values() if f["decompiled"])
        print(f"\nIndexed {len(functions_data)} functions ({decompiled_count} decompiled)")
        return

    # Phase 2: Parallel disassembly of functions that need embedding
    print("Disassembling functions...", file=sys.stderr)

    def disassemble_task(task):
        func_name, unit_name, target_path, source_path = task
        asm = disassemble_function(target_path, func_name)
        if not asm:
            return None
        processed_asm = preprocess_asm_for_embedding(asm)
        if not processed_asm:
            return None  # Empty function
        decompiled = is_function_implemented(source_path, func_name, [])
        return (func_name, {
            "unit": unit_name,
            "source": str(source_path),
            "asm": processed_asm,
            "decompiled": decompiled,
            "embedding": None,
        })

    to_embed = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=DISASSEMBLY_WORKERS) as executor:
        futures = [executor.submit(disassemble_task, task) for task in functions_to_embed]
        for future in as_completed(futures):
            result = future.result()
            if result:
                func_name, data = result
                to_embed[func_name] = data
            completed += 1
            if completed % 500 == 0:
                print(f"  Disassembled {completed}/{len(functions_to_embed)}", file=sys.stderr)

    print(f"  Disassembled {len(to_embed)} functions", file=sys.stderr)

    # Phase 3: Generate embeddings with concurrent API requests
    print("Generating embeddings...", file=sys.stderr)
    func_names = list(to_embed.keys())

    # Create batches with character budget (long functions get smaller batches)
    # Voyage limit is ~120k tokens per request; estimate ~4 chars per token
    batches = []
    current_batch_names = []
    current_batch_asm = []
    current_chars = 0
    batch_idx = 0

    for name in func_names:
        asm = to_embed[name]["asm"]
        asm_len = len(asm)

        # If single function exceeds budget, send it alone
        if asm_len > VOYAGE_BATCH_CHAR_BUDGET:
            if current_batch_names:
                batches.append((batch_idx, current_batch_names, current_batch_asm))
                batch_idx += 1
                current_batch_names = []
                current_batch_asm = []
                current_chars = 0
            batches.append((batch_idx, [name], [asm]))
            batch_idx += 1
            continue

        # If adding this would exceed budget or count limit, flush current batch
        if (current_chars + asm_len > VOYAGE_BATCH_CHAR_BUDGET or
                len(current_batch_names) >= VOYAGE_BATCH_SIZE):
            if current_batch_names:
                batches.append((batch_idx, current_batch_names, current_batch_asm))
                batch_idx += 1
            current_batch_names = []
            current_batch_asm = []
            current_chars = 0

        current_batch_names.append(name)
        current_batch_asm.append(asm)
        current_chars += asm_len

    # Don't forget the last batch
    if current_batch_names:
        batches.append((batch_idx, current_batch_names, current_batch_asm))

    # Process batches with concurrent API calls
    lock = threading.Lock()
    embedded_count = 0
    error_count = 0

    def embed_batch(batch_info):
        nonlocal embedded_count, error_count
        batch_idx, batch_names, batch_asm = batch_info
        try:
            embeddings = voyage_embed_batch(batch_asm)
            with lock:
                for name, emb in zip(batch_names, embeddings):
                    to_embed[name]["embedding"] = emb
                embedded_count += len(batch_names)
            return True
        except Exception as e:
            with lock:
                error_count += len(batch_names)
            print(f"  Error at batch {batch_idx}: {e}", file=sys.stderr)
            return False

    with ThreadPoolExecutor(max_workers=VOYAGE_CONCURRENT_REQUESTS) as executor:
        futures = [executor.submit(embed_batch, batch) for batch in batches]
        for i, future in enumerate(as_completed(futures)):
            future.result()  # Propagate any unhandled exceptions
            if (i + 1) % 5 == 0 or i == len(batches) - 1:
                print(f"  {embedded_count}/{len(func_names)} embedded", file=sys.stderr)

    # Merge newly embedded functions into final data
    for func_name, data in to_embed.items():
        if data["embedding"]:
            del data["asm"]  # Remove asm before saving
            functions_data[func_name] = data

    # Save cache
    save_embeddings_cache(functions_data)

    decompiled_count = sum(1 for f in functions_data.values() if f["decompiled"])
    print(f"\nIndexed {len(functions_data)} functions ({decompiled_count} decompiled)")
    if error_count:
        print(f"  ({error_count} functions failed to embed)", file=sys.stderr)
    print(f"Saved to {EMBEDDINGS_CACHE}")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def cmd_similar(func_name: str, top_n: int = 10, decompiled_only: bool = False):
    """Find functions most similar to the given function."""
    cache = load_embeddings_cache()
    if not cache:
        print("No embeddings cache. Run 'tools.py index' first.", file=sys.stderr)
        sys.exit(1)

    functions = cache["functions"]

    if func_name not in functions:
        # Try partial match
        matches = [n for n in functions if func_name.lower() in n.lower()]
        if len(matches) == 1:
            func_name = matches[0]
        elif matches:
            print(f"Ambiguous name. Matches: {matches[:5]}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Function {func_name} not in embeddings cache", file=sys.stderr)
            sys.exit(1)

    target_emb = functions[func_name]["embedding"]
    if not target_emb:
        print(f"Function {func_name} has no embedding", file=sys.stderr)
        sys.exit(1)

    # Compute similarities
    similarities = []
    for name, data in functions.items():
        if name == func_name:
            continue
        if decompiled_only and not data["decompiled"]:
            continue
        if not data["embedding"]:
            continue

        sim = cosine_similarity(target_emb, data["embedding"])
        similarities.append((name, sim, data))

    similarities.sort(key=lambda x: -x[1])

    # Output
    print(f"Functions similar to {func_name}:\n")
    print(f"{'Score':>6} {'Status':>8} | Function")
    print(f"{'-----':>6} {'------':>8} | --------")

    for name, sim, data in similarities[:top_n]:
        status = "DONE" if data["decompiled"] else "TODO"
        print(f"{sim:>6.3f} {status:>8} | {name}")
        print(f"{'':>6} {'':>8} | └─ {data['unit']}")
        print()




def cmd_recommend(top_n: int = 20, min_similarity: float = 0.85):
    """Find undecompiled functions with the most similar decompiled neighbors.

    These are the best candidates to work on because you have reference
    implementations nearby to learn from.

    Computes fresh every time (fast with numpy, ~10 seconds).
    Auto-syncs the cache if needed.
    """
    if not HAS_NUMPY:
        print("Error: numpy required for recommend. Install with: pip install numpy", file=sys.stderr)
        sys.exit(1)

    # Try to load cache, fail if stale (no auto-sync - it's expensive)
    cache = load_embeddings_cache()
    if not cache:
        stale_cache = load_embeddings_cache(ignore_hash=True)
        if stale_cache:
            print("Embeddings cache is stale (objdiff.json changed).", file=sys.stderr)
            print("Run 'tools.py index --refresh' to update (costs money, takes 30+ min).", file=sys.stderr)
            print("Using stale cache anyway...", file=sys.stderr)
            cache = stale_cache
        else:
            print("No embeddings cache. Run 'tools.py index' first.", file=sys.stderr)
            sys.exit(1)

    # Warn if cache is old (suggest rebuild if > 7 days)
    age_days = get_embeddings_cache_age_days()
    if age_days and age_days > 7:
        print(f"Note: Embeddings cache is {age_days:.0f} days old.", file=sys.stderr)
        print("      Consider running 'index --refresh' if many new functions were added.", file=sys.stderr)
        print()

    functions = cache["functions"]

    # Separate decompiled and undecompiled functions
    dec_names = []
    dec_embeddings = []
    undec_names = []
    undec_embeddings = []
    undec_units = []

    for name, data in functions.items():
        if not data["embedding"]:
            continue
        if data["decompiled"]:
            dec_names.append(name)
            dec_embeddings.append(data["embedding"])
        else:
            undec_names.append(name)
            undec_embeddings.append(data["embedding"])
            undec_units.append(data["unit"])

    print(f"Computing similarities for {len(undec_names)} undecompiled functions (numpy accelerated)...", file=sys.stderr)

    # Use numpy for fast matrix cosine similarity
    dec_matrix = np.array(dec_embeddings)  # (n_dec, dim)
    undec_matrix = np.array(undec_embeddings)  # (n_undec, dim)

    # Normalize vectors
    dec_norms = np.linalg.norm(dec_matrix, axis=1, keepdims=True)
    undec_norms = np.linalg.norm(undec_matrix, axis=1, keepdims=True)
    dec_normalized = dec_matrix / dec_norms
    undec_normalized = undec_matrix / undec_norms

    # Compute all similarities at once: (n_undec, n_dec)
    similarities_matrix = undec_normalized @ dec_normalized.T

    print(f"Finding best matches...", file=sys.stderr)

    # For each undecompiled function, find best decompiled neighbors
    candidates = []
    for i, (func_name, unit) in enumerate(zip(undec_names, undec_units)):
        sims = similarities_matrix[i]

        # Find indices above threshold
        above_threshold = sims >= min_similarity
        if not np.any(above_threshold):
            continue

        # Get indices sorted by similarity
        top_indices = np.argsort(sims)[::-1][:5]
        best_idx = top_indices[0]
        best_sim = float(sims[best_idx])
        neighbor_count = int(np.sum(above_threshold))

        candidates.append({
            "name": func_name,
            "unit": unit,
            "best_sim": best_sim,
            "avg_sim": float(np.mean(sims[top_indices])),
            "neighbor_count": neighbor_count,
            "top_neighbor": dec_names[best_idx],
        })

    # Sort by best similarity (functions with very similar decompiled neighbors first)
    candidates.sort(key=lambda x: -x["best_sim"])

    print(f"\nRecommended functions to decompile (best reference material):\n")
    print(f"{'Best':>6} {'#Refs':>6} | Function")
    print(f"{'----':>6} {'-----':>6} | --------")

    for c in candidates[:top_n]:
        print(f"{c['best_sim']:>6.3f} {c['neighbor_count']:>6} | {c['name']}")
        print(f"{'':>6} {'':>6} | └─ {c['unit']}")
        print(f"{'':>6} {'':>6} | └─ Most similar: {c['top_neighbor']}")
        print()


def get_target_asm_for_m2c(func_name: str) -> tuple[str | None, str | None]:
    """Get target assembly in m2c format from the .s file.

    Returns (asm_text, unit_name) or (None, None) if not found.
    """
    units = load_units()

    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        # Check if function exists in this unit
        asm = disassemble_function(target_path, func_name)
        if not asm:
            continue

        # Find the .s file
        target_rel = target_path.relative_to(MELEE_REPO / "build/GALE01")
        s_path = MELEE_REPO / "build/GALE01/asm" / str(target_rel).replace("obj/", "").replace(".o", ".s")

        if not s_path.exists():
            continue

        content = s_path.read_text()

        # Find function in .s file
        func_pattern = rf'^\.fn\s+{re.escape(func_name)}\s*,\s*\w+'
        func_match = re.search(func_pattern, content, re.MULTILINE)
        if not func_match:
            continue

        start = func_match.end()

        # Find function end
        end_match = re.search(r'^\.(?:fn|endfn)\b', content[start:], re.MULTILINE)
        if end_match:
            func_content = content[start:start + end_match.start()]
        else:
            func_content = content[start:]

        # Convert to m2c format (glabel + instructions)
        asm_lines = [f"glabel {func_name}"]
        for line in func_content.split('\n'):
            line = line.strip()
            if not line:
                continue

            # Keep labels (they already end with : in some cases)
            if line.startswith('.L_'):
                label = line.rstrip(':')
                asm_lines.append(label + ":")
                continue

            # Skip other directives
            if line.startswith('.'):
                continue

            # Extract instruction from: /* ADDR OFFSET */ instruction
            match = re.match(r'/\*.*\*/\s+(.+)', line)
            if match:
                insn = match.group(1).strip()
                asm_lines.append(insn)

        return '\n'.join(asm_lines), unit_name

    return None, None


def cmd_m2c(func_name: str):
    """Get m2c decompilation for a function (works for stubbed functions).

    Uses local m2c installation for fast decompilation.
    """
    units = load_units()

    # Find the function's assembly file
    asm_file = None
    unit_name = None

    for uname, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        # Check if function exists in this unit's target
        asm = disassemble_function(target_path, func_name)
        if asm:
            # Find the .s file
            # target_path: build/GALE01/obj/melee/lb/lbshadow.o
            # asm_path: build/GALE01/asm/melee/lb/lbshadow.s
            rel_path = target_path.relative_to(MELEE_REPO / "build/GALE01/obj")
            asm_file = MELEE_REPO / "build/GALE01/asm" / rel_path.with_suffix(".s")
            unit_name = uname
            break

    if not asm_file or not asm_file.exists():
        print(f"Function {func_name} not found or no assembly file", file=sys.stderr)
        sys.exit(1)

    # Run local m2c
    try:
        result = subprocess.run(
            ["m2c", "-t", "ppc-mwcc-c", "-f", func_name, str(asm_file)],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0 or not result.stdout.strip():
            print(f"m2c failed for {func_name}", file=sys.stderr)
            if result.stderr:
                print(result.stderr[:500], file=sys.stderr)
            sys.exit(1)

        decompilation = result.stdout.strip()

        print(f"// Function: {func_name}")
        print(f"// Unit: {unit_name}")
        print(f"// Source: {asm_file}")
        print()
        print(decompilation)

        return decompilation

    except FileNotFoundError:
        print("m2c not found. Install with: pip install git+https://github.com/matt-kempster/m2c.git", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"m2c timed out for {func_name}", file=sys.stderr)
        sys.exit(1)


# =============================================================================
# Type Inference Commands
# =============================================================================

MELEE_SRC = MELEE_REPO / "src" / "melee"


def infer_base_type(var_name: str, content: str):
    """Try to infer the type of a variable from declarations or casts."""
    patterns = [
        rf'\(([A-Z]\w+)\s*\*\s*{var_name}\)',
        rf'([A-Z]\w+)\s*\*\s*{var_name}\s*[=;]',
        rf'{var_name}\s*=\s*\(([A-Z]\w+)\s*\*\)',
        rf'{var_name}\s*=\s*GET_FIGHTER',
    ]

    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            if 'GET_FIGHTER' in pattern:
                return 'Fighter'
            return match.group(1)

    if var_name in ('fp', 'fighter', 'ft'):
        return 'Fighter'
    if var_name in ('gobj', 'fighter_gobj'):
        return 'HSD_GObj'
    if var_name in ('jobj',):
        return 'HSD_JObj'
    if var_name in ('ip', 'item'):
        return 'Item'

    return None


def cmd_template(func_name: str):
    """Show template code from the most similar matched function."""
    print(f"Finding template for {func_name}...", file=sys.stderr)

    # Get similar decompiled functions using our existing similarity search
    cache = load_embeddings_cache()
    if not cache:
        print("No embeddings cache. Run 'tools.py index' first.", file=sys.stderr)
        sys.exit(1)

    functions = cache["functions"]
    if func_name not in functions:
        matches = [n for n in functions if func_name.lower() in n.lower()]
        if len(matches) == 1:
            func_name = matches[0]
        elif matches:
            print(f"Ambiguous name. Matches: {matches[:5]}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Function {func_name} not in cache", file=sys.stderr)
            sys.exit(1)

    target_emb = functions[func_name]["embedding"]
    if not target_emb:
        print(f"Function {func_name} has no embedding", file=sys.stderr)
        sys.exit(1)

    # Find most similar decompiled function
    best_match = None
    best_sim = 0
    for name, data in functions.items():
        if name == func_name or not data["decompiled"] or not data["embedding"]:
            continue
        sim = cosine_similarity(target_emb, data["embedding"])
        if sim > best_sim:
            best_sim = sim
            best_match = name

    if not best_match:
        print("No similar decompiled function found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nMost similar matched function: {best_match} ({best_sim:.1%} similar)")
    print("=" * 70)

    # Search for the function in source files
    for c_file in MELEE_SRC.rglob("*.c"):
        content = c_file.read_text(errors='ignore')
        pattern = rf'^\s*(?:static\s+)?(?:inline\s+)?\w+[\s\*]+{re.escape(best_match)}\s*\([^)]*\)\s*\{{'
        match = re.search(pattern, content, re.MULTILINE)
        if match:
            start = match.start()
            brace_count = 0
            in_func = False
            end = start
            for i, char in enumerate(content[start:], start):
                if char == '{':
                    brace_count += 1
                    in_func = True
                elif char == '}':
                    brace_count -= 1
                    if in_func and brace_count == 0:
                        end = i + 1
                        break

            func_code = content[start:end]
            print(f"\nSource: {c_file.relative_to(MELEE_REPO)}")
            print("-" * 70)
            print(func_code)
            print("-" * 70)

            # Show target assembly
            print(f"\nTarget assembly for {func_name}:")
            print("-" * 70)
            # Get assembly using existing pattern from cmd_asm
            units = load_units()
            for unit_name, unit in units.items():
                target_path = MELEE_REPO / unit.get("target_path", "")
                if not target_path.exists():
                    continue
                asm = disassemble_function(target_path, func_name)
                if asm:
                    lines = asm.strip().split('\n')[:50]
                    print('\n'.join(lines))
                    if len(asm.strip().split('\n')) > 50:
                        print("... (truncated)")
                    break
            return

    print(f"Could not find source for {best_match}", file=sys.stderr)


def cmd_infer(struct_filter: str = None):
    """Infer types for unknown struct fields based on usage patterns."""
    from collections import Counter

    print("Scanning matched source files...", file=sys.stderr)

    all_results = {}
    file_count = 0

    for c_file in MELEE_SRC.rglob("*.c"):
        content = c_file.read_text(errors='ignore')

        # Extract cast patterns: (Type)var->field
        cast_pattern = r'\((\w+\s*\*?)\)\s*(\w+)->(\w+)'
        for match in re.finditer(cast_pattern, content):
            cast_type = match.group(1).strip()
            base_var = match.group(2)
            field = match.group(3)
            base_type = infer_base_type(base_var, content)

            if base_type and (not struct_filter or struct_filter.lower() in base_type.lower()):
                key = (base_type, field)
                if key not in all_results:
                    all_results[key] = {'casts': [], 'comparisons': []}
                all_results[key]['casts'].append(cast_type)

        # Extract comparison patterns: var->field == 0.0F
        compare_pattern = r'(\w+)->(\w+)\s*[<>=!]+\s*([0-9.]+F?|NULL|true|false)'
        for match in re.finditer(compare_pattern, content):
            base_var = match.group(1)
            field = match.group(2)
            literal = match.group(3)
            base_type = infer_base_type(base_var, content)

            if base_type and (not struct_filter or struct_filter.lower() in base_type.lower()):
                key = (base_type, field)
                if key not in all_results:
                    all_results[key] = {'casts': [], 'comparisons': []}

                if 'F' in literal or '.' in literal:
                    all_results[key]['comparisons'].append('float')
                elif literal in ('NULL', 'true', 'false'):
                    all_results[key]['comparisons'].append('ptr_or_bool')

        file_count += 1

    print(f"Analyzed {file_count} files", file=sys.stderr)

    # Print report for unknown fields (x123 style)
    print("\n" + "=" * 70)
    print("TYPE INFERENCE REPORT")
    print("=" * 70)

    by_struct = {}
    for (struct_type, field), data in all_results.items():
        if not re.match(r'x[0-9A-Fa-f]+', field):
            continue
        if struct_type not in by_struct:
            by_struct[struct_type] = {}
        by_struct[struct_type][field] = data

    for struct_type in sorted(by_struct.keys()):
        print(f"\n{struct_type}:")
        print("-" * 40)

        fields = by_struct[struct_type]
        for field in sorted(fields.keys(), key=lambda f: int(re.match(r'x([0-9A-Fa-f]+)', f).group(1), 16) if re.match(r'x([0-9A-Fa-f]+)', f) else 0):
            data = fields[field]
            inferred = None
            evidence = []

            if data['casts']:
                cast_counts = Counter(data['casts'])
                most_common = cast_counts.most_common(1)[0]
                inferred = most_common[0]
                evidence.append(f"cast to {inferred} ({most_common[1]}x)")

            if data['comparisons']:
                comp_counts = Counter(data['comparisons'])
                if 'float' in comp_counts:
                    if not inferred:
                        inferred = 'float'
                    evidence.append(f"compared as float ({comp_counts['float']}x)")

            if inferred:
                print(f"  {field:20} -> {inferred:12}")
                for ev in evidence[:2]:
                    print(f"    └─ {ev}")


def cmd_field_usage(struct_type: str, field_name: str):
    """Show all usages of a specific struct field."""
    print(f"Finding usages of {struct_type}->{field_name}...", file=sys.stderr)

    usages = []
    for c_file in MELEE_SRC.rglob("*.c"):
        content = c_file.read_text(errors='ignore')
        pattern = rf'(\w+)->{re.escape(field_name)}\b'

        for match in re.finditer(pattern, content):
            base_var = match.group(1)
            base_type = infer_base_type(base_var, content)

            if base_type and base_type.lower() == struct_type.lower():
                line_start = content.rfind('\n', 0, match.start()) + 1
                line_end = content.find('\n', match.end())
                if line_end == -1:
                    line_end = len(content)
                line = content[line_start:line_end].strip()
                line_num = content[:match.start()].count('\n') + 1
                usages.append((c_file, line_num, line))

    print(f"\nUsages of {struct_type}->{field_name} ({len(usages)} found):\n")
    for file_path, line_num, line in usages[:30]:
        rel_path = file_path.relative_to(MELEE_REPO)
        print(f"{rel_path}:{line_num}")
        print(f"  {line[:100]}")
        print()


def cmd_suggest(func_name: str):
    """Analyze assembly to suggest field types from instructions."""
    print(f"Analyzing {func_name}...", file=sys.stderr)

    # Find function using existing pattern from cmd_asm
    units = load_units()
    asm = None
    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue
        asm = disassemble_function(target_path, func_name)
        if asm:
            break

    if not asm:
        print(f"Function {func_name} not found in any unit", file=sys.stderr)
        sys.exit(1)

    # Extract offset patterns
    offset_pattern = r'(?:lwz|stw|lfs|stfs|lbz|stb|lhz|sth)\s+r\d+,\s*(\d+)\(r(\d+)\)'
    offsets_used = {}

    for match in re.finditer(offset_pattern, asm):
        offset = int(match.group(1))
        instr = match.group(0).split()[0]
        if offset not in offsets_used:
            offsets_used[offset] = []
        offsets_used[offset].append(instr)

    print(f"\nOffsets accessed in {func_name}:\n")
    print(f"{'Offset':>8} | {'Instructions':25} | Likely Type")
    print(f"{'-'*8} | {'-'*25} | {'-'*12}")

    for offset in sorted(offsets_used.keys()):
        instrs = offsets_used[offset]
        instr_str = ', '.join(sorted(set(instrs)))

        likely_type = "u32/s32/ptr"
        if any(i in ['lfs', 'stfs'] for i in instrs):
            likely_type = "float"
        elif any(i in ['lbz', 'stb'] for i in instrs):
            likely_type = "u8/s8"
        elif any(i in ['lhz', 'sth'] for i in instrs):
            likely_type = "u16/s16"

        print(f"  0x{offset:04X} | {instr_str:25} | {likely_type}")


# =============================================================================
# Sandbox & Vacuum Commands
# =============================================================================

SANDBOXES_DIR = MELEE_AI / "sandboxes"
DIFFICULT_FUNCTIONS = MELEE_AI / ".difficult_functions"
SUBMITTED_PRS = MELEE_AI / ".submitted_prs"
SANDBOX_TEMPLATE = MELEE_AI / "sandbox_claude.md"


def load_skip_list() -> set[str]:
    """Load the set of functions to skip (difficult + submitted PRs)."""
    skipped = set()

    # Load difficult functions
    if DIFFICULT_FUNCTIONS.exists():
        for line in DIFFICULT_FUNCTIONS.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            func_name = line.split()[0]
            skipped.add(func_name)

    # Load submitted PRs
    if SUBMITTED_PRS.exists():
        for line in SUBMITTED_PRS.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                skipped.add(parts[1])

    return skipped


def cmd_sandbox(func_name: str):
    """Create an isolated sandbox environment for decompiling a function.

    Creates melee-ai/sandboxes/<func_name>/ with all files needed for
    autonomous decompilation: base.c, template.c, target.s, build.sh, etc.
    """
    import shutil

    units = load_units()

    # Find the function's unit
    func_unit = None
    func_unit_name = None
    target_path = None

    for unit_name, unit in units.items():
        tp = MELEE_REPO / unit.get("target_path", "")
        if not tp.exists():
            continue
        asm = disassemble_function(tp, func_name)
        if asm:
            func_unit = unit
            func_unit_name = unit_name
            target_path = tp
            break

    if not func_unit:
        print(f"Function {func_name} not found in any unit", file=sys.stderr)
        sys.exit(1)

    # Create sandbox directory
    sandbox_dir = SANDBOXES_DIR / func_name
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write target.s (assembly from .s file or objdump)
    target_asm = get_target_asm_from_s_file(func_unit, func_name)
    if target_asm:
        (sandbox_dir / "target.s").write_text(f"# Target assembly for {func_name}\n"
                                               f"# Unit: {func_unit_name}\n\n"
                                               f"{target_asm}\n")
    else:
        # Fallback to objdump
        asm = disassemble_function(target_path, func_name)
        (sandbox_dir / "target.s").write_text(f"# Target assembly for {func_name}\n"
                                               f"# Unit: {func_unit_name}\n\n"
                                               f"{asm}\n")

    # 2. Copy target.o
    shutil.copy2(target_path, sandbox_dir / "target.o")

    # 3. Generate base.c via m2c
    try:
        # Find the .s file for m2c
        rel_path = target_path.relative_to(MELEE_REPO / "build/GALE01/obj")
        asm_file = MELEE_REPO / "build/GALE01/asm" / rel_path.with_suffix(".s")

        if asm_file.exists():
            result = subprocess.run(
                ["m2c", "-t", "ppc-mwcc-c", "-f", func_name, str(asm_file)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                (sandbox_dir / "base.c").write_text(
                    f"// m2c decompilation of {func_name}\n"
                    f"// Unit: {func_unit_name}\n\n"
                    f"{result.stdout.strip()}\n"
                )
            else:
                (sandbox_dir / "base.c").write_text(
                    f"// Decompile {func_name}\n"
                    f"// Unit: {func_unit_name}\n"
                    f"// m2c failed - write from scratch using target.s and template.c\n\n"
                    f"void {func_name}(void) {{\n"
                    f"    // TODO: implement\n"
                    f"}}\n"
                )
        else:
            (sandbox_dir / "base.c").write_text(
                f"// Decompile {func_name}\n"
                f"// Unit: {func_unit_name}\n\n"
                f"void {func_name}(void) {{\n"
                f"    // TODO: implement from target.s\n"
                f"}}\n"
            )
    except FileNotFoundError:
        (sandbox_dir / "base.c").write_text(
            f"// Decompile {func_name}\n"
            f"// m2c not available - implement from target.s and template.c\n\n"
            f"void {func_name}(void) {{\n"
            f"    // TODO: implement\n"
            f"}}\n"
        )

    # 4. Generate template.c (from most similar matched function) and similar_functions.txt
    try:
        cache = load_embeddings_cache()
        if cache and func_name in cache["functions"]:
            target_emb = cache["functions"][func_name]["embedding"]
            if target_emb:
                # Collect all similar decompiled functions
                similarities = []
                for name, data in cache["functions"].items():
                    if name == func_name or not data["decompiled"] or not data["embedding"]:
                        continue
                    sim = cosine_similarity(target_emb, data["embedding"])
                    similarities.append((name, sim, data.get("unit", "unknown")))

                # Sort by similarity and take top 5
                similarities.sort(key=lambda x: -x[1])
                top_similar = similarities[:5]

                # Write similar_functions.txt with the list
                if top_similar:
                    similar_lines = [
                        f"# Similar decompiled functions for {func_name}",
                        f"# Use these as additional references if template.c doesn't help.",
                        f"# To find source: grep -r 'function_name' src/melee/",
                        ""
                    ]
                    for name, sim, unit in top_similar:
                        similar_lines.append(f"{sim:.1%} | {name}")
                        similar_lines.append(f"      | └─ {unit}")
                    (sandbox_dir / "similar_functions.txt").write_text("\n".join(similar_lines) + "\n")

                best_match = top_similar[0][0] if top_similar else None
                best_sim = top_similar[0][1] if top_similar else 0

                if best_match:
                    # Extract the matched function's source code
                    template_code = None
                    for c_file in MELEE_SRC.rglob("*.c"):
                        content = c_file.read_text(errors='ignore')
                        pattern = rf'^\s*(?:static\s+)?(?:inline\s+)?\w+[\s\*]+{re.escape(best_match)}\s*\([^)]*\)\s*\{{'
                        match = re.search(pattern, content, re.MULTILINE)
                        if match:
                            start = match.start()
                            brace_count = 0
                            in_func = False
                            end = start
                            for i, char in enumerate(content[start:], start):
                                if char == '{':
                                    brace_count += 1
                                    in_func = True
                                elif char == '}':
                                    brace_count -= 1
                                    if in_func and brace_count == 0:
                                        end = i + 1
                                        break
                            template_code = content[start:end]
                            (sandbox_dir / "template.c").write_text(
                                f"// Template: {best_match} ({best_sim:.1%} similar)\n"
                                f"// Source: {c_file.relative_to(MELEE_REPO)}\n\n"
                                f"{template_code}\n"
                            )
                            break

                    if not template_code:
                        (sandbox_dir / "template.c").write_text(
                            f"// Most similar matched function: {best_match} ({best_sim:.1%})\n"
                            f"// Source code not found in source tree.\n"
                        )
                else:
                    (sandbox_dir / "template.c").write_text(
                        f"// No similar matched function found for {func_name}.\n"
                    )
            else:
                (sandbox_dir / "template.c").write_text(
                    f"// No embedding available for {func_name}.\n"
                )
        else:
            (sandbox_dir / "template.c").write_text(
                f"// No embeddings cache available.\n"
                f"// Run: tools.py index --refresh\n"
            )
    except Exception as e:
        (sandbox_dir / "template.c").write_text(
            f"// Error generating template: {e}\n"
        )

    # 4b. Generate extra_context.h with real extern/typedef declarations from source
    source_path = MELEE_REPO / func_unit.get("metadata", {}).get("source_path", "")
    extra_ctx_lines = []
    if source_path.exists():
        src_text = source_path.read_text(errors='ignore')
        header_path = source_path.with_suffix('.h')
        hdr_text = header_path.read_text(errors='ignore') if header_path.exists() else ""

        for text in [src_text, hdr_text]:
            # Extract typedef struct blocks
            for m in re.finditer(
                r'^(typedef\s+struct\b[^;]*?\{.*?^\}\s*\w+\s*;)',
                text, re.MULTILINE | re.DOTALL
            ):
                extra_ctx_lines.append(m.group(0))

            # Extract extern declarations
            for m in re.finditer(r'^(extern\s+[^;]+;)', text, re.MULTILINE):
                extra_ctx_lines.append(m.group(0))

    # Deduplicate (same extern may appear in both .c and .h)
    seen = set()
    deduped = []
    for line in extra_ctx_lines:
        if line not in seen:
            seen.add(line)
            deduped.append(line)
    extra_ctx_lines = deduped

    extra_ctx = ""
    if extra_ctx_lines:
        extra_ctx = (
            f"/* Extra context from {source_path.name} - real extern/typedef declarations */\n\n"
            + "\n\n".join(extra_ctx_lines)
            + "\n"
        )
    (sandbox_dir / "extra_context.h").write_text(extra_ctx)

    # 5. Symlink context.txt
    ctx_link = sandbox_dir / "context.txt"
    if ctx_link.exists() or ctx_link.is_symlink():
        ctx_link.unlink()
    if CONTEXT_TXT.exists():
        ctx_link.symlink_to(CONTEXT_TXT)
    else:
        # Try the standard location
        alt_ctx = Path.home() / "melee-decomp-agent" / "context.txt"
        if alt_ctx.exists():
            ctx_link.symlink_to(alt_ctx)

    # 6. Generate build.sh
    build_sh = f'''#!/bin/bash
# Build and score a decompilation attempt for {func_name}
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT="${{1:-base.c}}"

if [ ! -f "$SCRIPT_DIR/$INPUT" ]; then
    echo "ERROR: $INPUT not found"
    exit 1
fi

MELEE_DIR="{MELEE_REPO}"
CTX="$SCRIPT_DIR/context.txt"
TARGET_O="$SCRIPT_DIR/target.o"
OUTPUT_O="/tmp/sandbox_{func_name}_$$.o"
TEMP_C="/tmp/sandbox_{func_name}_$$.c"

# Build: context (with target function's declaration filtered out) + extra context + source
# Filter out the target function's forward declaration to avoid type conflicts
grep -v '{func_name}' "$CTX" > "$TEMP_C"
echo "" >> "$TEMP_C"
# Include real extern/typedef declarations from the source file
if [ -s "$SCRIPT_DIR/extra_context.h" ]; then
    cat "$SCRIPT_DIR/extra_context.h" >> "$TEMP_C"
    echo "" >> "$TEMP_C"
fi
cat "$SCRIPT_DIR/$INPUT" >> "$TEMP_C"

# Compile with mwcc
cd "$MELEE_DIR"
"$MELEE_DIR/build/tools/wibo" "$MELEE_DIR/build/tools/sjiswrap.exe" \\
    "$MELEE_DIR/build/compilers/GC/1.2.5n/mwcceppc.exe" \\
    -nowraplines -cwd source -Cpp_exceptions off -proc gekko \\
    -fp hardware -align powerpc -nosyspath -fp_contract on \\
    -O4,p -multibyte -enum int -nodefaults -inline auto \\
    -pragma 'cats off' -pragma 'warn_notinlined off' \\
    -RTTI off -str reuse -DBUILD_VERSION=0 -DVERSION_GALE01 \\
    -DM2CTX -DNDEBUG=1 -maxerrors 1 -msgstyle std -warn off \\
    -i src -i src/MSL -i src/Runtime -i extern/dolphin/include \\
    -i src/melee -i src/melee/ft/chara -i src/sysdolphin \\
    -lang=c -c "$TEMP_C" -o "$OUTPUT_O"
COMPILE_RC=$?

rm -f "$TEMP_C"

if [ $COMPILE_RC -ne 0 ]; then
    echo "COMPILATION FAILED. Fix compiler errors above."
    rm -f "$OUTPUT_O"
    exit 1
fi

# Score using tools.py score command
cd "$SCRIPT_DIR"
SANDBOX_INPUT="$INPUT" python3 "{MELEE_AI / 'tools.py'}" score "{func_name}" "$TARGET_O" "$OUTPUT_O"
SCORE_RC=$?

rm -f "$OUTPUT_O"
exit $SCORE_RC
'''
    (sandbox_dir / "build.sh").write_text(build_sh)
    (sandbox_dir / "build.sh").chmod(0o755)

    # 7. Generate diff.sh
    diff_sh = f'''#!/bin/bash
# Show assembly diff for a compiled attempt
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT="${{1:-base.o}}"

if [ ! -f "$SCRIPT_DIR/$INPUT" ]; then
    echo "ERROR: $INPUT not found. Run build.sh first."
    exit 1
fi

OBJDUMP="{OBJDUMP}"
if [ ! -f "$OBJDUMP" ]; then
    OBJDUMP="powerpc-linux-gnu-objdump"
fi

echo "=== TARGET ==="
"$OBJDUMP" -d "$SCRIPT_DIR/target.o" | grep -A 1000 "<{func_name}>:" | head -100

echo ""
echo "=== CURRENT ==="
"$OBJDUMP" -d "$SCRIPT_DIR/$INPUT" | grep -A 1000 "<{func_name}>:" | head -100
'''
    (sandbox_dir / "diff.sh").write_text(diff_sh)
    (sandbox_dir / "diff.sh").chmod(0o755)

    # 8. Create match_log.txt
    (sandbox_dir / "match_log.txt").write_text("")

    # 9. Generate CLAUDE.md from template with field offset hints
    if SANDBOX_TEMPLATE.exists():
        template = SANDBOX_TEMPLATE.read_text()
        claude_md = template.replace("{func_name}", func_name)
    else:
        claude_md = f"# Decompile: {func_name}\n\nSee build.sh and template.c.\n"

    # Extract field offset hints from assembly
    asm_text = (sandbox_dir / "target.s").read_text()
    offset_pattern = r'(?:lwz|stw|lfs|stfs|lbz|stb|lhz|sth|lfd|stfd)\s+\w+,\s*(-?(?:0x[0-9a-fA-F]+|\d+))\(r(\d+)\)'
    offsets_by_reg = {}
    for m in re.finditer(offset_pattern, asm_text):
        offset_str = m.group(1)
        offset = int(offset_str, 16) if offset_str.startswith(('0x', '0X', '-0x', '-0X')) else int(offset_str)
        reg = m.group(2)
        if reg == '1':  # Skip stack frame (r1) accesses
            continue
        instr = m.group(0).split()[0]
        key = (reg, offset)
        if key not in offsets_by_reg:
            offsets_by_reg[key] = set()
        offsets_by_reg[key].add(instr)

    if offsets_by_reg:
        hint_lines = ["\n## Field Offset Hints\n",
                      "These offsets are accessed in the target assembly. "
                      "Cross-reference with struct definitions to find correct field names.\n",
                      f"| {'Offset':>8} | {'Reg':>4} | {'Instructions':25} | {'Likely Type'} |",
                      f"| {'-'*8} | {'-'*4} | {'-'*25} | {'-'*12} |"]
        for (reg, offset) in sorted(offsets_by_reg.keys(), key=lambda x: (x[0], x[1])):
            instrs = offsets_by_reg[(reg, offset)]
            instr_str = ', '.join(sorted(instrs))
            likely_type = "u32/s32/ptr"
            if instrs & {'lfs', 'stfs'}:
                likely_type = "float"
            elif instrs & {'lfd', 'stfd'}:
                likely_type = "double"
            elif instrs & {'lbz', 'stb'}:
                likely_type = "u8/s8"
            elif instrs & {'lhz', 'sth'}:
                likely_type = "u16/s16"
            hint_lines.append(f"| 0x{offset:04X} | r{reg:>3} | {instr_str:25} | {likely_type:12} |")
        claude_md += "\n" + "\n".join(hint_lines) + "\n"

    (sandbox_dir / "CLAUDE.md").write_text(claude_md)

    print(f"Sandbox created: {sandbox_dir}")
    print()
    print("Files:")
    for f in sorted(sandbox_dir.iterdir()):
        size = f.stat().st_size if not f.is_symlink() else 0
        kind = " -> " + str(f.resolve()) if f.is_symlink() else ""
        print(f"  {f.name:20s} {size:>8d}b{kind}")
    print()
    print(f"To decompile manually:")
    print(f"  cd {sandbox_dir}")
    print(f"  cat template.c   # Reference code")
    print(f"  cat target.s     # Target assembly")
    print(f"  # Edit base.c, then:")
    print(f"  ./build.sh base.c")


def cmd_score(func_name: str, target_o_path: str, current_o_path: str):
    """Score two object files and report match percentage.

    Used by sandbox build.sh to check match quality.
    Writes results to match_log.txt if in a sandbox.
    """
    target_o = Path(target_o_path)
    current_o = Path(current_o_path)

    if not target_o.exists():
        print(f"Target object not found: {target_o}", file=sys.stderr)
        sys.exit(1)
    if not current_o.exists():
        print(f"Current object not found: {current_o}", file=sys.stderr)
        sys.exit(1)

    score, max_score, diff_lines = score_object_files(target_o, current_o, func_name)

    if max_score > 0:
        match_pct = (1 - score / max_score) * 100
    else:
        match_pct = 100.0 if score == 0 else 0.0

    print(f"Match: {match_pct:.2f}% (score: {score}/{max_score})")

    if diff_lines:
        print()
        print("Differences:")
        for line in diff_lines[:40]:
            print(line)

    # Write to match_log.txt if we're being called from a sandbox
    # (detect by checking if build.sh is invoking us)
    cwd = Path.cwd()
    match_log = cwd / "match_log.txt"
    if match_log.exists() or (cwd.parent == SANDBOXES_DIR):
        # Try to detect which base_N.c was used from argv or env
        input_name = os.environ.get("SANDBOX_INPUT", "unknown.c")
        with open(match_log, "a") as f:
            f.write(f"{input_name} {match_pct:.2f}%\n")

    print()
    if match_pct >= 100.0:
        print(f"PERFECT MATCH. This function matches the target binary.")
    elif match_pct >= 99.0:
        print(f"VERY CLOSE ({match_pct:.2f}%). Minor register allocation differences remain.")
    elif match_pct >= 90.0:
        print(f"NOT MATCHED ({match_pct:.2f}%). Analyse the diff and fix remaining issues.")
    else:
        print(f"NOT MATCHED ({match_pct:.2f}%). Major structural differences. Review control flow.")


def _extract_function_signature(code: str, func_name: str):
    """Extract return type and parameter list from a function definition.

    Returns (return_type, params_str) or None if not found.
    e.g. ("long", "s32 amount") or ("void", "Item_GObj* gobj, s32* msid, Vec3* pos")
    """
    # Match: [static] [inline] return_type func_name(params)
    # Use [^\n] to avoid matching across lines (comments before function)
    pattern = rf'^(?:static\s+)?(?:inline\s+)?([\w][\w\s\*]*?)\s+{re.escape(func_name)}\s*\(([^)]*)\)'
    m = re.search(pattern, code, re.MULTILINE)
    if not m:
        return None
    ret_type = m.group(1).strip()
    params = m.group(2).strip()
    return (ret_type, params)


def _update_header_prototype(source_path, func_name: str, ret_type: str, params: str):
    """Update the function prototype in the corresponding header file.

    Finds the header (.h) for the given source (.c) and replaces any
    UNK_RET/UNK_PARAMS stub prototype with the real signature.

    Only updates if all types in the new prototype are already present
    in the header file (to avoid introducing unknown types that break
    other translation units).
    """
    header_path = source_path.with_suffix('.h')
    if not header_path.exists():
        return False

    header = header_path.read_text()

    # Match prototype line like: /* ADDR */ UNK_RET func_name(UNK_PARAMS);
    proto_pattern = rf'(/\*.*?\*/\s+)\S[\w\s\*]*?\s+{re.escape(func_name)}\s*\([^)]*\)\s*;'
    m = re.search(proto_pattern, header)
    if not m:
        return False

    old_proto = m.group(0)
    addr_comment = m.group(1)  # e.g. "/* 162B4C */ "

    # Safety check: only use types that are basic C types or already present
    # in the header file. This prevents introducing unknown types that break
    # other translation units that include this header.
    basic_types = {
        'void', 'int', 'char', 'short', 'long', 'float', 'double',
        'signed', 'unsigned', 'const', 'volatile', 'struct', 'enum',
        'static', 'inline', 'extern', 'typedef', 'union',
        's8', 's16', 's32', 's64', 'u8', 'u16', 'u32', 'u64',
        'f32', 'f64', 'bool', 'BOOL', 'size_t',
        'UNK_RET', 'UNK_PARAMS', 'UNK_T', 'M2C_UNK',
    }

    def _types_safe(text):
        """Check if all type identifiers in text are safe to use in header."""
        tokens = set(re.findall(r'\b(\w+)\b', text))
        for token in tokens:
            if token in basic_types or token in header or token == func_name:
                continue
            return False
        return True

    # Check return type safety
    ret_safe = _types_safe(ret_type)
    if not ret_safe:
        print(f"Skipping header prototype update: return type '{ret_type}' has unknown types")
        return False

    # Check param types safety - if unsafe, skip update entirely.
    # mwcc treats func() as "no arguments" (not "unspecified" like C89 standard),
    # so empty parens cause build failures when the function takes arguments.
    # Leaving UNK_PARAMS intact is safest since it's a macro the build handles.
    params_safe = _types_safe(params)
    if params_safe:
        new_params = params
    else:
        print(f"Skipping header prototype update: params '{params}' have unknown types")
        return False

    # Build new prototype
    new_proto = f"{addr_comment}{ret_type} {func_name}({new_params});"

    if old_proto == new_proto:
        return False  # Already correct

    new_header = header.replace(old_proto, new_proto)
    header_path.write_text(new_header)
    print(f"Updated header prototype in {header_path}")
    print(f"  old: {old_proto.strip()}")
    print(f"  new: {new_proto.strip()}")
    return True


def cmd_integrate(func_name: str):
    """Integrate a matched function from a sandbox into the source tree.

    Reads the best matched base_N.c from the sandbox and replaces the
    stub/existing code in the source file.
    Also updates the header prototype if it uses UNK_RET/UNK_PARAMS stubs.
    """
    sandbox_dir = SANDBOXES_DIR / func_name

    if not sandbox_dir.exists():
        print(f"No sandbox found for {func_name}", file=sys.stderr)
        sys.exit(1)

    # Find the best matched file
    match_log = sandbox_dir / "match_log.txt"
    best_file = None
    best_pct = 0.0

    if match_log.exists():
        for line in match_log.read_text().strip().split('\n'):
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    pct = float(parts[1].rstrip('%'))
                    if pct > best_pct:
                        best_pct = pct
                        best_file = parts[0]
                except ValueError:
                    continue

    if best_pct < 50.0:
        print(f"No 50%+ match found in sandbox (best: {best_pct:.1f}%)", file=sys.stderr)
        print(f"NOT VERIFIED. DO NOT integrate non-matching code.", file=sys.stderr)
        sys.exit(1)

    if not best_file:
        print(f"No match log entries found", file=sys.stderr)
        sys.exit(1)

    matched_file = sandbox_dir / best_file
    if not matched_file.exists():
        print(f"Matched file not found: {matched_file}", file=sys.stderr)
        sys.exit(1)

    matched_code = matched_file.read_text().strip()

    # Extract BSS extern declarations (un_XXXXXXXX) - these need to be added
    # to the source file since they're not declared in any header.
    # Strip other externs which may conflict with real header declarations.
    # Also strip sandbox metadata comments (// Decompilation of, // Unit:, etc.)
    matched_lines = matched_code.split('\n')
    bss_externs = []
    clean_lines = []
    for line in matched_lines:
        stripped = line.strip()
        # Strip sandbox metadata comments
        if re.match(r'^//\s*(?:Decompilation of|Unit:|m2c decompilation of)\s*', stripped):
            continue
        if re.match(r'^\s*extern\s+', line):
            # Keep BSS variable externs (un_804XXXXX pattern)
            if re.search(r'\bun_[0-9A-Fa-f]{8}\b', line):
                bss_externs.append(line.strip())
            # Strip all other externs (may conflict with headers)
        else:
            clean_lines.append(line)
    matched_code = '\n'.join(clean_lines).strip()

    # Find the source file
    units = load_units()
    func_unit = None
    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue
        asm = disassemble_function(target_path, func_name)
        if asm:
            func_unit = unit
            break

    if not func_unit:
        print(f"Function {func_name} not found in any unit", file=sys.stderr)
        sys.exit(1)

    source_path = MELEE_REPO / func_unit.get("metadata", {}).get("source_path", "")
    if not source_path.exists():
        print(f"Source file not found: {source_path}", file=sys.stderr)
        sys.exit(1)

    content = source_path.read_text()

    # Add BSS extern declarations if not already present
    if bss_externs:
        added_externs = []
        for ext in bss_externs:
            # Extract variable name from extern declaration
            var_match = re.search(r'\b(un_[0-9A-Fa-f]{8})\b', ext)
            if var_match:
                var_name = var_match.group(1)
                # Check if already declared in source
                if not re.search(rf'\b{re.escape(var_name)}\b', content.split('/// #')[0]):
                    added_externs.append(ext)

        if added_externs:
            # Find insertion point: after last #include or after initial block
            # Look for first function stub marker or function definition
            insert_match = re.search(r'\n(///\s*#\w+|(?:static\s+)?(?:inline\s+)?\w+\s+\w+\s*\()', content)
            if insert_match:
                insert_pos = insert_match.start()
                extern_block = '\n' + '\n'.join(added_externs) + '\n'
                content = content[:insert_pos] + extern_block + content[insert_pos:]
                print(f"Added BSS externs: {', '.join(re.search(r'un_[0-9A-Fa-f]+', e).group() for e in added_externs)}")

    # Guard: check if function already exists in source (avoid duplicate integration)
    already_exists = rf'(?:^|\n)\s*(?:static\s+)?(?:inline\s+)?\w[\w\s\*]*\s+{re.escape(func_name)}\s*\('
    if re.search(already_exists, content):
        # Function already present - check if it looks like a real implementation
        func_def_pattern = rf'(?:static\s+)?(?:inline\s+)?\w[\w\s\*]*\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{'
        m = re.search(func_def_pattern, content)
        if m:
            # Find closing brace to measure body size
            start = m.start()
            brace_count = 0
            in_func = False
            end = start
            for i, char in enumerate(content[start:], start):
                if char == '{':
                    brace_count += 1
                    in_func = True
                elif char == '}':
                    brace_count -= 1
                    if in_func and brace_count == 0:
                        end = i + 1
                        break
            body = content[start:end]
            # If body is non-trivial (not just empty braces), it's already integrated
            body_inner = body[body.index('{') + 1:body.rindex('}')].strip()
            if body_inner and body_inner != 'return;':
                print(f"Function {func_name} already exists in {source_path}")
                print(f"Skipping duplicate integration.")
                return

    # Extract signature from matched code for header update
    sig = _extract_function_signature(matched_code, func_name)

    # Try to find and replace the stub marker: /// #func_name
    stub_pattern = rf'///\s*#{re.escape(func_name)}\b[^\n]*'
    if re.search(stub_pattern, content):
        new_content = re.sub(stub_pattern, matched_code, content)
        source_path.write_text(new_content)
        print(f"Replaced stub marker with matched code in {source_path}")
        print(f"Function: {func_name}")
        print(f"Source: {source_path}")
        if sig:
            _update_header_prototype(source_path, func_name, sig[0], sig[1])
        return

    # Try to find existing function definition and replace it
    func_pattern = rf'((?:static\s+)?(?:inline\s+)?\w[\w\s\*]*\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{)'
    match = re.search(func_pattern, content)
    if match:
        start = match.start()
        brace_count = 0
        in_func = False
        end = start
        for i, char in enumerate(content[start:], start):
            if char == '{':
                brace_count += 1
                in_func = True
            elif char == '}':
                brace_count -= 1
                if in_func and brace_count == 0:
                    end = i + 1
                    break

        new_content = content[:start] + matched_code + content[end:]
        source_path.write_text(new_content)
        print(f"Replaced existing function in {source_path}")
        print(f"Function: {func_name}")
        if sig:
            _update_header_prototype(source_path, func_name, sig[0], sig[1])
        return

    # No stub marker or existing function - append after the last function
    # Find the right insertion point by address ordering.
    # Extract address from func_name (e.g., mnGallery_80259604 -> 80259604)
    addr_match = re.search(r'[_]([0-9A-Fa-f]{8})$', func_name)
    if addr_match:
        target_addr = int(addr_match.group(1), 16)
    else:
        target_addr = 0xFFFFFFFF  # Append at end

    # Find all function/stub addresses in the file to determine insertion point
    # Look for patterns: /// #name_ADDR or function_ADDR(
    addr_positions = []
    for m in re.finditer(r'///\s*#\w+?_([0-9A-Fa-f]{8})\b', content):
        addr = int(m.group(1), 16)
        addr_positions.append((addr, m.start()))
    for m in re.finditer(r'\n\w[\w\s\*]*\s+\w+?_([0-9A-Fa-f]{8})\s*\(', content):
        addr = int(m.group(1), 16)
        addr_positions.append((addr, m.start()))

    # Sort by address and find where to insert
    addr_positions.sort(key=lambda x: x[0])

    insert_pos = len(content)  # Default: end of file
    for addr, pos in addr_positions:
        if addr > target_addr:
            # Insert before this function/stub
            # Back up to the start of the line
            line_start = content.rfind('\n', 0, pos)
            insert_pos = line_start + 1 if line_start >= 0 else pos
            break

    # Insert the matched code with surrounding newlines
    new_code = f"\n{matched_code}\n"
    new_content = content[:insert_pos] + new_code + content[insert_pos:]
    source_path.write_text(new_content)
    print(f"Appended function at address-ordered position in {source_path}")
    print(f"Function: {func_name}")
    print(f"Source: {source_path}")
    if sig:
        _update_header_prototype(source_path, func_name, sig[0], sig[1])


def compute_difficulty_score(asm: str) -> float:
    """Compute a difficulty heuristic from assembly text.

    Returns a value between 0.0 (easy) and 1.0 (hard).
    Based on instruction count, branch complexity, and function calls.
    """
    lines = [l.strip() for l in asm.split('\n') if l.strip()]

    # Count features
    insn_count = 0
    branch_count = 0
    call_count = 0
    label_count = 0

    for line in lines:
        # Skip non-instruction lines
        if line.endswith(':') or line.startswith('#') or line.startswith('.'):
            if line.endswith(':'):
                label_count += 1
            continue

        # Extract instruction mnemonic
        match = re.match(r'^\s*(?:[0-9a-f]+:\s+[0-9a-f ]+\s+)?(\w+)', line, re.I)
        if not match:
            continue

        mnemonic = match.group(1).lower()
        insn_count += 1

        if mnemonic.startswith('b') and mnemonic not in ('blr',):
            if mnemonic == 'bl':
                call_count += 1
            else:
                branch_count += 1

    if insn_count == 0:
        return 1.0

    # Normalize features (larger = harder)
    # Short functions are easier
    size_factor = min(insn_count / 200.0, 1.0)

    # More branches = more complex control flow
    branch_ratio = min(branch_count / max(insn_count, 1) * 5.0, 1.0)

    # More function calls = more complex interactions
    call_ratio = min(call_count / max(insn_count, 1) * 8.0, 1.0)

    # Weighted combination
    difficulty = (
        0.40 * size_factor +
        0.35 * branch_ratio +
        0.25 * call_ratio
    )

    return min(max(difficulty, 0.0), 1.0)


def scan_target_functions() -> dict[str, dict]:
    """Scan all target objects to build a map of valid functions.

    Returns {func_name: {"unit": str, "target_path": Path, "insn_count": int}}.
    This is the source of truth for what functions actually exist in the build.
    """
    units = load_units()
    objdump = get_objdump_cmd()
    func_map = {}

    for unit_name, unit in units.items():
        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        # Get symbol table for function names and sizes
        try:
            result = subprocess.run(
                [str(objdump), "-t", str(target_path)],
                capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            continue

        for line in result.stdout.split("\n"):
            if " F " not in line:
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            func_name = parts[-1]
            if func_name.startswith("gap_"):
                continue
            try:
                size_bytes = int(parts[4], 16) if len(parts[4]) <= 8 else 0
            except ValueError:
                size_bytes = 0

            # Approximate instruction count (each PPC instruction is 4 bytes)
            insn_count = size_bytes // 4

            func_map[func_name] = {
                "unit": unit_name,
                "target_path": target_path,
                "insn_count": insn_count,
            }

    return func_map


def cmd_vacuum_pick(top_n: int = 10, min_similarity: float = 0.90,
                     max_instructions: int = 200, unit_filter: str = None):
    """Pick the best candidates for autonomous decompilation.

    Combines embedding similarity (good reference material) with
    difficulty scoring (simpler functions first) to find the
    easiest functions to decompile autonomously.

    Excludes functions in the skip list, submitted PRs, and functions
    that don't exist in target objects (stale cache entries).
    """
    if not HAS_NUMPY:
        print("Error: numpy required. Install with: pip install numpy", file=sys.stderr)
        sys.exit(1)

    # Load cache (no auto-sync - it's expensive)
    cache = load_embeddings_cache()
    if not cache:
        stale_cache = load_embeddings_cache(ignore_hash=True)
        if stale_cache:
            print("Embeddings cache is stale. Using anyway...", file=sys.stderr)
            cache = stale_cache
        else:
            print("No embeddings cache. Run 'tools.py index' first.", file=sys.stderr)
            sys.exit(1)

    functions = cache["functions"]
    skip_list = load_skip_list()

    # Build valid function set from target objects (source of truth)
    print("Scanning target objects...", file=sys.stderr)
    valid_funcs = scan_target_functions()
    print(f"Found {len(valid_funcs)} functions in target objects", file=sys.stderr)

    # Separate decompiled and undecompiled, filtering against valid set
    dec_names = []
    dec_embeddings = []
    undec_names = []
    undec_embeddings = []
    undec_units = []
    stale_count = 0

    for name, data in functions.items():
        if not data["embedding"]:
            continue
        if data["decompiled"]:
            dec_names.append(name)
            dec_embeddings.append(data["embedding"])
        else:
            if name in skip_list:
                continue
            if name not in valid_funcs:
                stale_count += 1
                continue
            # Filter by instruction count early
            if valid_funcs[name]["insn_count"] > max_instructions:
                continue
            if unit_filter and unit_filter not in data.get("unit", ""):
                continue
            undec_names.append(name)
            undec_embeddings.append(data["embedding"])
            undec_units.append(data["unit"])

    if stale_count > 0:
        print(f"Filtered {stale_count} stale functions (not in target objects)", file=sys.stderr)

    # Filter out functions already implemented in source files (catches worktree commits
    # that the embeddings cache doesn't know about yet)
    units = load_units()
    unit_source_cache = {}  # Cache source file contents per unit
    implemented_count = 0
    filtered_names = []
    filtered_embeddings = []
    filtered_units = []

    for name, emb, unit in zip(undec_names, undec_embeddings, undec_units):
        # Get source path for this unit
        if unit not in unit_source_cache:
            unit_data = units.get(unit, {})
            source_path = MELEE_REPO / unit_data.get("metadata", {}).get("source_path", "")
            unit_source_cache[unit] = source_path

        source_path = unit_source_cache[unit]
        if source_path.exists() and is_function_implemented(source_path, name, []):
            implemented_count += 1
            continue

        filtered_names.append(name)
        filtered_embeddings.append(emb)
        filtered_units.append(unit)

    undec_names = filtered_names
    undec_embeddings = filtered_embeddings
    undec_units = filtered_units

    if implemented_count > 0:
        print(f"Filtered {implemented_count} already-implemented functions", file=sys.stderr)

    if not undec_names:
        print("No candidates available (all skipped, decompiled, or filtered).", file=sys.stderr)
        sys.exit(1)

    if not dec_names:
        print("No decompiled functions for reference.", file=sys.stderr)
        sys.exit(1)

    print(f"Scoring {len(undec_names)} candidates (excluded {len(skip_list)} skipped, "
          f"max {max_instructions} insns)...", file=sys.stderr)

    # Compute similarity matrix
    dec_matrix = np.array(dec_embeddings)
    undec_matrix = np.array(undec_embeddings)

    dec_norms = np.linalg.norm(dec_matrix, axis=1, keepdims=True)
    undec_norms = np.linalg.norm(undec_matrix, axis=1, keepdims=True)
    dec_normalized = dec_matrix / dec_norms
    undec_normalized = undec_matrix / undec_norms

    similarities_matrix = undec_normalized @ dec_normalized.T

    # Score candidates using pre-computed instruction counts
    candidates = []
    for i, (func_name, unit) in enumerate(zip(undec_names, undec_units)):
        sims = similarities_matrix[i]
        best_idx = np.argmax(sims)
        best_sim = float(sims[best_idx])

        if best_sim < min_similarity:
            continue

        insn_count = valid_funcs[func_name]["insn_count"]

        # Difficulty from instruction count (no objdump needed)
        size_factor = min(insn_count / 200.0, 1.0)
        difficulty = size_factor  # Simple: just use size

        # Vacuum score: high similarity + low difficulty = best candidate
        vacuum_score = best_sim * (1.0 - difficulty * 0.6)

        candidates.append({
            "name": func_name,
            "unit": unit,
            "best_sim": best_sim,
            "difficulty": difficulty,
            "insn_count": insn_count,
            "vacuum_score": vacuum_score,
            "top_neighbor": dec_names[best_idx],
        })

    candidates.sort(key=lambda x: -x["vacuum_score"])

    print(f"\n# Vacuum candidates (easiest with best references):\n")
    print(f"{'VScore':>7} {'Sim':>5} {'Insns':>5} | Function")
    print(f"{'------':>7} {'---':>5} {'-----':>5} | --------")

    for c in candidates[:top_n]:
        print(f"{c['vacuum_score']:>7.3f} {c['best_sim']:>5.3f} {c['insn_count']:>5} | {c['name']}")
        print(f"{'':>7} {'':>5} {'':>5} | └─ {c['unit']}")
        print(f"{'':>7} {'':>5} {'':>5} | └─ ref: {c['top_neighbor']}")
        print()


def cmd_setup_worktree(name: str = None):
    """Set up a fresh vacuum worktree with all dependencies.

    Creates a new git worktree with orig copied and binutils symlinked,
    runs configure.py and ninja, and prints the path for use with vacuum.

    Usage:
        tools.py setup-worktree [name]   - Create worktree (default: vacuum-YYYYMMDD)
    """
    from datetime import datetime

    # Determine worktree name and path
    if not name:
        name = f"vacuum-{datetime.now().strftime('%Y%m%d')}"

    worktrees_dir = MELEE_AI.parent / ".worktrees"
    worktree_path = worktrees_dir / name
    main_repo = MELEE_AI.parent / "melee"

    if worktree_path.exists():
        print(f"Worktree already exists: {worktree_path}")
        print(f"Use: --melee-repo {worktree_path}")
        return

    print(f"Setting up worktree: {worktree_path}")

    # Create worktrees directory
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Create git worktree
    branch_name = f"vacuum/{name}"
    result = subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
        cwd=main_repo, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Failed to create worktree: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"  Created branch: {branch_name}")

    # Copy orig directory
    orig_src = main_repo / "orig"
    orig_dst = worktree_path / "orig"
    if orig_src.exists():
        subprocess.run(["cp", "-r", str(orig_src), str(orig_dst)], check=True)
        print("  Copied orig directory")
    else:
        print(f"  Warning: {orig_src} not found, skipping")

    # Symlink binutils
    build_dir = worktree_path / "build"
    build_dir.mkdir(exist_ok=True)
    binutils_src = main_repo / "build" / "binutils"
    binutils_dst = build_dir / "binutils"
    if binutils_src.exists() and not binutils_dst.exists():
        binutils_dst.symlink_to(binutils_src)
        print("  Symlinked binutils")

    # Run configure.py
    print("  Running configure.py...")
    result = subprocess.run(
        ["python3", "configure.py"],
        cwd=worktree_path, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  Warning: configure.py failed: {result.stderr[:200]}")

    # Run ninja
    print("  Running ninja (this may take a few minutes)...")
    result = subprocess.run(
        ["ninja"],
        cwd=worktree_path, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  Warning: ninja failed: {result.stderr[:500]}")
    else:
        # Show progress
        for line in result.stdout.splitlines()[-10:]:
            if "Progress" in line or "%" in line:
                print(f"  {line}")

    print()
    print(f"Worktree ready: {worktree_path}")
    print()
    print("Run vacuum with:")
    print(f"  python3 melee-ai/vacuum.py --max 15 --melee-repo {worktree_path}")
    print()
    print("Or with relaxed parameters:")
    print(f"  python3 melee-ai/vacuum.py --max 100 --relaxed --melee-repo {worktree_path}")


def cmd_skip(func_name: str = None, reason: str = "", clean: bool = False):
    """Manage the difficult_functions skip list.

    Usage:
        tools.py skip <func> [reason]   - Add function to skip list
        tools.py skip --clean            - Remove matched functions from list
    """
    if clean:
        if not DIFFICULT_FUNCTIONS.exists():
            print("No skip list to clean.")
            return

        units = load_units()
        lines = DIFFICULT_FUNCTIONS.read_text().splitlines()
        kept = []
        removed = 0

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                kept.append(line)
                continue

            fname = stripped.split()[0]

            # Check if function is now matched
            matched = False
            for unit_name, unit in units.items():
                source_path = MELEE_REPO / unit.get("metadata", {}).get("source_path", "")
                if source_path.exists() and is_function_implemented(source_path, fname, []):
                    matched = True
                    break

            if matched:
                removed += 1
            else:
                kept.append(line)

        DIFFICULT_FUNCTIONS.write_text('\n'.join(kept) + '\n')
        print(f"Removed {removed} matched functions from skip list.")
        return

    if not func_name:
        # List current skip list
        if not DIFFICULT_FUNCTIONS.exists():
            print("Skip list is empty.")
            return

        print("Skipped functions:")
        for line in DIFFICULT_FUNCTIONS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                print(f"  {line}")
        return

    # Add to skip list
    date = datetime.now().strftime("%Y-%m-%d")
    entry = f"{func_name} {date}"
    if reason:
        entry += f" reason:{reason}"
    entry += "\n"

    with open(DIFFICULT_FUNCTIONS, "a") as f:
        f.write(entry)

    print(f"Added {func_name} to skip list.")


def main():
    if len(sys.argv) < 2:
        print("Usage: tools.py <command> [args]")
        print()
        print("Commands:")
        print("  recommend [N]        - Find best functions to work on (have similar refs)")
        print("  similar <func> [N]   - Find N most similar functions (default: 10)")
        print("  similar <func> --decompiled - Only show decompiled functions")
        print("  template <func>      - Show code from most similar matched function")
        print("  asm <function>       - Get target assembly (objdump)")
        print("  m2c <function>       - Get m2c decompilation (works for stubs)")
        print("  source <unit>        - Show source file path")
        print("  scratch <function>   - Test against target (local mwcc)")
        print("  permute <function>   - Set up permuter for register allocation search")
        print("  verify <function>    - Check official report.json (AUTHORITATIVE)")
        print()
        print("Vacuum commands:")
        print("  setup-worktree [name] - Set up fresh worktree for vacuum runs")
        print("  vacuum-pick [N]      - Pick best candidates for autonomous decompilation")
        print("  sandbox <function>   - Create isolated sandbox for decompilation")
        print("  integrate <function> - Integrate matched sandbox code into source tree")
        print("  skip <function>      - Add function to skip list")
        print("  skip --clean         - Remove matched functions from skip list")
        print("  score <func> <tgt.o> <cur.o> - Score two object files (used by sandbox)")
        print()
        print("Analysis commands:")
        print("  infer [--struct X]   - Infer types for unknown fields from usage")
        print("  suggest <func>       - Analyze assembly to suggest field types")
        print("  field-usage <struct> <field> - Show all usages of a struct field")
        print()
        print("Other commands:")
        print("  list                 - List incomplete units")
        print("  funcs <unit>         - List functions in a unit")
        print("  index [--refresh]    - Rebuild embeddings cache (slow, uses Voyage API)")
        print("  index --sync         - Update decompiled flags only (fast)")
        print()
        print("WORKFLOW (manual):")
        print("  1. recommend         - Find functions with best reference material")
        print("  2. template <func>   - Get reference code from similar matched function")
        print("  3. source <unit>     - Find source file, add the code")
        print("  4. scratch <func>    - Test locally (fast, ~0.1s)")
        print("  5. permute <func>    - If stuck on regalloc (>95%), run permuter")
        print("  6. ninja + verify    - Final verification before commit")
        print()
        print("WORKFLOW (vacuum):")
        print("  python3 melee-ai/vacuum.py [--max N] [--timeout SECS] [--dry-run]")
        print()
        print("NOTE: scratch uses local mwcc compiler (~0.1s per test).")
        print("      template shows C code from the most similar matched function.")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "asm" and len(sys.argv) >= 3:
        cmd_asm(sys.argv[2])
    elif cmd == "m2c" and len(sys.argv) >= 3:
        cmd_m2c(sys.argv[2])
    elif cmd == "scratch" and len(sys.argv) >= 3:
        cmd_scratch(sys.argv[2])
    elif cmd == "permute" and len(sys.argv) >= 3:
        func_name = sys.argv[2]
        iterations = 0
        for arg in sys.argv[3:]:
            if arg.startswith("--iterations="):
                iterations = int(arg.split("=")[1])
            elif arg.isdigit():
                iterations = int(arg)
        cmd_permute(func_name, iterations)
    elif cmd == "source" and len(sys.argv) >= 3:
        cmd_source(sys.argv[2])
    elif cmd == "list":
        cmd_list()
    elif cmd == "funcs" and len(sys.argv) >= 3:
        cmd_funcs(sys.argv[2])
    elif cmd == "verify" and len(sys.argv) >= 3:
        cmd_verify(sys.argv[2])
    elif cmd == "index":
        refresh = "--refresh" in sys.argv
        sync = "--sync" in sys.argv
        cmd_index(refresh=refresh, sync=sync)
    elif cmd == "recommend":
        top_n = 20
        for arg in sys.argv[2:]:
            if arg.isdigit():
                top_n = int(arg)
        cmd_recommend(top_n=top_n)
    elif cmd == "similar" and len(sys.argv) >= 3:
        func_name = sys.argv[2]
        top_n = 10
        decompiled_only = "--decompiled" in sys.argv
        for arg in sys.argv[3:]:
            if arg.isdigit():
                top_n = int(arg)
        cmd_similar(func_name, top_n, decompiled_only)
    elif cmd == "template" and len(sys.argv) >= 3:
        cmd_template(sys.argv[2])
    elif cmd == "infer":
        struct_filter = None
        for i, arg in enumerate(sys.argv[2:]):
            if arg == "--struct" and i + 1 < len(sys.argv) - 2:
                struct_filter = sys.argv[i + 3]
        cmd_infer(struct_filter)
    elif cmd == "suggest" and len(sys.argv) >= 3:
        cmd_suggest(sys.argv[2])
    elif cmd == "field-usage" and len(sys.argv) >= 4:
        cmd_field_usage(sys.argv[2], sys.argv[3])
    elif cmd == "sandbox" and len(sys.argv) >= 3:
        cmd_sandbox(sys.argv[2])
    elif cmd == "integrate" and len(sys.argv) >= 3:
        cmd_integrate(sys.argv[2])
    elif cmd == "vacuum-pick":
        top_n = 10
        min_sim = 0.90
        max_insns = 50
        unit_filt = None
        args_iter = iter(sys.argv[2:])
        for arg in args_iter:
            if arg.startswith("--min-similarity="):
                min_sim = float(arg.split("=")[1])
            elif arg == "--min-similarity":
                min_sim = float(next(args_iter))
            elif arg.startswith("--max-instructions="):
                max_insns = int(arg.split("=")[1])
            elif arg == "--max-instructions":
                max_insns = int(next(args_iter))
            elif arg.startswith("--unit="):
                unit_filt = arg.split("=")[1]
            elif arg == "--unit":
                unit_filt = next(args_iter)
            elif arg.isdigit():
                top_n = int(arg)
        cmd_vacuum_pick(top_n=top_n, min_similarity=min_sim,
                        max_instructions=max_insns, unit_filter=unit_filt)
    elif cmd == "skip":
        clean = "--clean" in sys.argv
        if clean:
            cmd_skip(clean=True)
        elif len(sys.argv) >= 3:
            func_name = sys.argv[2]
            reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
            cmd_skip(func_name=func_name, reason=reason)
        else:
            cmd_skip()
    elif cmd == "score" and len(sys.argv) >= 5:
        cmd_score(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "setup-worktree":
        name = sys.argv[2] if len(sys.argv) >= 3 else None
        cmd_setup_worktree(name)
    else:
        print(f"Unknown command or missing args: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
