#!/usr/bin/env python3
"""
Type inference for Melee decompilation.

Analyzes matched C source files to infer field types based on usage patterns.
This helps fill in unknown struct fields (like x123, x456) with actual types.

Techniques used:
1. Assignment analysis: What type is assigned to a field?
2. Parameter analysis: What type does a function expect for this field?
3. Return analysis: What function returns this value?
4. Operation analysis: What operations are performed (float ops, comparisons)?
5. Cast analysis: What explicit casts are used?
"""

import json
import os
import re
import sys
from collections import defaultdict, Counter
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

# Paths
MELEE_REPO = Path(__file__).parent.parent / "melee"
MELEE_SRC = MELEE_REPO / "src" / "melee"
CONTEXT_TXT = MELEE_REPO.parent / "context.txt"
CACHE_FILE = Path(__file__).parent / ".type_inference_cache.json"


def find_matched_functions(src_dir: Path) -> Dict[str, Path]:
    """Find all matched functions by scanning source files."""
    matched = {}
    for c_file in src_dir.rglob("*.c"):
        content = c_file.read_text(errors='ignore')
        # Functions with actual implementations (not just stubs)
        # Look for function definitions that aren't just ASM_FN stubs
        for match in re.finditer(r'^([a-zA-Z_]\w*)\s*\([^)]*\)\s*\{', content, re.MULTILINE):
            func_name = match.group(1)
            if not func_name.startswith('ASM_') and not func_name.startswith('fn_'):
                matched[func_name] = c_file
    return matched


def extract_field_accesses(content: str) -> List[Tuple[str, str, str, str]]:
    """
    Extract field access patterns from C code.

    Returns list of (base_type, field_name, context_type, context_info)
    where context_type is one of: 'assignment', 'param', 'return', 'cast', 'operation'
    """
    accesses = []

    # Pattern 1: Direct field access with arrow operator
    # e.g., fp->x2C, gobj->user_data, fighter->co_attrs.grav
    arrow_pattern = r'(\w+)->(\w+(?:\.\w+)*)'

    # Pattern 2: Cast expressions
    # e.g., (Fighter*)gobj->user_data, (s32)fp->x123
    cast_pattern = r'\((\w+\s*\*?)\)\s*(\w+)->(\w+)'

    # Pattern 3: Function call with field as argument
    # e.g., SomeFunc(fp->x123) - we want to know what type SomeFunc expects
    func_call_pattern = r'(\w+)\s*\([^)]*(\w+)->(\w+)[^)]*\)'

    # Pattern 4: Assignment to field
    # e.g., fp->x123 = value; or var = fp->x123;
    assign_left_pattern = r'(\w+)->(\w+)\s*='
    assign_right_pattern = r'(\w+)\s*=\s*(\w+)->(\w+)'

    # Pattern 5: Comparison with literals
    # e.g., fp->x123 == 0, fp->x123 != NULL, fp->x123 < 1.0F
    compare_pattern = r'(\w+)->(\w+)\s*[<>=!]+\s*([0-9.]+F?|NULL|true|false)'

    # Extract arrow accesses with context
    for match in re.finditer(arrow_pattern, content):
        base = match.group(1)
        field = match.group(2)
        # Get surrounding context (50 chars each side)
        start = max(0, match.start() - 50)
        end = min(len(content), match.end() + 50)
        context = content[start:end]
        accesses.append((base, field, 'access', context))

    # Extract casts
    for match in re.finditer(cast_pattern, content):
        cast_type = match.group(1).strip()
        base = match.group(2)
        field = match.group(3)
        accesses.append((base, field, 'cast', cast_type))

    # Extract function calls
    for match in re.finditer(func_call_pattern, content):
        func_name = match.group(1)
        base = match.group(2)
        field = match.group(3)
        accesses.append((base, field, 'func_param', func_name))

    # Extract comparisons with literals
    for match in re.finditer(compare_pattern, content):
        base = match.group(1)
        field = match.group(2)
        literal = match.group(3)
        # Infer type from literal
        if 'F' in literal or '.' in literal:
            inferred = 'float'
        elif literal in ('NULL', 'true', 'false'):
            inferred = 'pointer_or_bool'
        else:
            inferred = 'integer'
        accesses.append((base, field, 'comparison', inferred))

    return accesses


def infer_base_type(var_name: str, content: str) -> Optional[str]:
    """Try to infer the type of a variable from declarations or casts."""
    # Common patterns
    patterns = [
        # Function parameter: (Type* var_name)
        rf'\(([A-Z]\w+)\s*\*\s*{var_name}\)',
        # Local declaration: Type* var_name =
        rf'([A-Z]\w+)\s*\*\s*{var_name}\s*[=;]',
        # Cast: (Type*)...assigned to var_name
        rf'{var_name}\s*=\s*\(([A-Z]\w+)\s*\*\)',
        # GET_FIGHTER macro
        rf'{var_name}\s*=\s*GET_FIGHTER',
    ]

    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            if 'GET_FIGHTER' in pattern:
                return 'Fighter'
            return match.group(1)

    # Heuristics based on common naming
    if var_name in ('fp', 'fighter', 'ft'):
        return 'Fighter'
    if var_name in ('gobj', 'fighter_gobj'):
        return 'HSD_GObj'
    if var_name in ('jobj',):
        return 'HSD_JObj'
    if var_name in ('ip', 'item'):
        return 'Item'

    return None


def analyze_file(file_path: Path) -> Dict[str, Dict[str, List]]:
    """
    Analyze a single C file for field access patterns.

    Returns dict mapping struct_type -> field_name -> list of (context_type, context_info)
    """
    content = file_path.read_text(errors='ignore')
    accesses = extract_field_accesses(content)

    # Group by inferred struct type and field
    results = defaultdict(lambda: defaultdict(list))

    for base_var, field, ctx_type, ctx_info in accesses:
        base_type = infer_base_type(base_var, content)
        if base_type:
            results[base_type][field].append((ctx_type, ctx_info))

    return results


def aggregate_type_info(all_results: Dict[str, Dict[str, List]]) -> Dict[str, Dict[str, Dict]]:
    """
    Aggregate field access info into type inferences.

    For each struct.field, compute:
    - Most likely type (based on casts, comparisons, function params)
    - Confidence score
    - Evidence (list of supporting observations)
    """
    aggregated = {}

    for struct_type, fields in all_results.items():
        if struct_type not in aggregated:
            aggregated[struct_type] = {}

        for field_name, observations in fields.items():
            # Count different types of evidence
            casts = []
            comparisons = []
            func_params = []

            for ctx_type, ctx_info in observations:
                if ctx_type == 'cast':
                    casts.append(ctx_info)
                elif ctx_type == 'comparison':
                    comparisons.append(ctx_info)
                elif ctx_type == 'func_param':
                    func_params.append(ctx_info)

            # Infer type
            inferred_type = None
            confidence = 0
            evidence = []

            # Casts are strongest evidence
            if casts:
                # Most common cast type
                cast_counts = Counter(casts)
                most_common = cast_counts.most_common(1)[0]
                inferred_type = most_common[0]
                confidence = min(most_common[1] / len(casts), 1.0) * 0.9
                evidence.append(f"cast to {inferred_type} ({most_common[1]}x)")

            # Comparisons help
            if comparisons:
                comp_types = Counter(comparisons)
                if comp_types.get('float', 0) > 0:
                    if not inferred_type:
                        inferred_type = 'float'
                        confidence = 0.7
                    evidence.append(f"compared as float ({comp_types['float']}x)")
                elif comp_types.get('pointer_or_bool', 0) > 0:
                    evidence.append(f"compared to NULL/bool ({comp_types['pointer_or_bool']}x)")

            # Store result
            aggregated[struct_type][field_name] = {
                'inferred_type': inferred_type,
                'confidence': confidence,
                'evidence': evidence,
                'observation_count': len(observations),
                'cast_count': len(casts),
                'comparison_count': len(comparisons),
            }

    return aggregated


def load_known_types(context_path: Path) -> Dict[str, Dict[str, str]]:
    """Load known struct definitions from context.txt."""
    if not context_path.exists():
        return {}

    content = context_path.read_text()
    known = defaultdict(dict)

    # Parse struct definitions
    # Very simplified - looks for offset comments and field declarations
    current_struct = None

    # Match struct Fighter { ... }
    struct_pattern = r'struct\s+(\w+)\s*\{([^}]+)\}'
    for match in re.finditer(struct_pattern, content, re.DOTALL):
        struct_name = match.group(1)
        struct_body = match.group(2)

        # Extract fields with offset comments
        # Pattern: /* 0xNN */ type field_name;
        field_pattern = r'/\*\s*(?:0x)?([0-9A-Fa-f]+)\s*\*/\s*(\w+(?:\s*\*)?)\s+(\w+)'
        for field_match in re.finditer(field_pattern, struct_body):
            offset = field_match.group(1)
            field_type = field_match.group(2).strip()
            field_name = field_match.group(3)
            known[struct_name][field_name] = field_type

    return known


def print_report(aggregated: Dict, known_types: Dict):
    """Print a report of inferred types, highlighting unknown fields."""
    print("=" * 80)
    print("TYPE INFERENCE REPORT")
    print("=" * 80)
    print()

    for struct_type in sorted(aggregated.keys()):
        fields = aggregated[struct_type]

        # Filter to fields that look like unknowns (x123, x456, etc.)
        unknown_fields = {
            f: d for f, d in fields.items()
            if re.match(r'x[0-9A-Fa-f]+(_[a-z0-9]+)?$', f)
        }

        if not unknown_fields:
            continue

        print(f"\n{struct_type}:")
        print("-" * 40)

        # Sort by confidence
        for field_name, data in sorted(unknown_fields.items(),
                                        key=lambda x: -x[1]['confidence']):
            if data['inferred_type']:
                conf_str = f"{data['confidence']:.0%}"
                print(f"  {field_name:20} -> {data['inferred_type']:12} ({conf_str})")
                for ev in data['evidence'][:2]:
                    print(f"    └─ {ev}")
            elif data['observation_count'] > 5:
                # Frequently used but couldn't infer type
                print(f"  {field_name:20} -> ???          (used {data['observation_count']}x)")


def cmd_infer(struct_filter: str = None, verbose: bool = False):
    """Run type inference on matched source files."""
    print("Scanning matched source files...", file=sys.stderr)

    all_results = defaultdict(lambda: defaultdict(list))
    file_count = 0

    for c_file in MELEE_SRC.rglob("*.c"):
        results = analyze_file(c_file)
        for struct_type, fields in results.items():
            if struct_filter and struct_filter.lower() not in struct_type.lower():
                continue
            for field_name, observations in fields.items():
                all_results[struct_type][field_name].extend(observations)
        file_count += 1

    print(f"Analyzed {file_count} files", file=sys.stderr)

    # Aggregate
    aggregated = aggregate_type_info(all_results)

    # Load known types for comparison
    known_types = load_known_types(CONTEXT_TXT)

    # Print report
    print_report(aggregated, known_types)

    # Summary stats
    total_fields = sum(len(f) for f in aggregated.values())
    inferred_fields = sum(
        1 for fields in aggregated.values()
        for data in fields.values()
        if data['inferred_type']
    )
    print(f"\nSummary: {inferred_fields}/{total_fields} fields with inferred types")


def cmd_usage(struct_type: str, field_name: str):
    """Show all usages of a specific struct field."""
    print(f"Finding usages of {struct_type}->{field_name}...", file=sys.stderr)

    usages = []

    for c_file in MELEE_SRC.rglob("*.c"):
        content = c_file.read_text(errors='ignore')

        # Find the field access
        pattern = rf'(\w+)->{re.escape(field_name)}\b'
        for match in re.finditer(pattern, content):
            base_var = match.group(1)
            base_type = infer_base_type(base_var, content)

            if base_type and base_type.lower() == struct_type.lower():
                # Get context (line containing the match)
                line_start = content.rfind('\n', 0, match.start()) + 1
                line_end = content.find('\n', match.end())
                if line_end == -1:
                    line_end = len(content)
                line = content[line_start:line_end].strip()

                # Get line number
                line_num = content[:match.start()].count('\n') + 1

                usages.append((c_file, line_num, line))

    print(f"\nUsages of {struct_type}->{field_name} ({len(usages)} found):\n")
    for file_path, line_num, line in usages[:50]:
        rel_path = file_path.relative_to(MELEE_REPO)
        print(f"{rel_path}:{line_num}")
        print(f"  {line[:100]}")
        print()


def cmd_patterns(struct_type: str = "Fighter"):
    """Find groups of fields that are commonly used together."""
    print(f"Finding field co-occurrence patterns for {struct_type}...", file=sys.stderr)

    # Track which fields appear together in functions
    field_cooccurrence = defaultdict(lambda: defaultdict(int))
    field_functions = defaultdict(set)

    for c_file in MELEE_SRC.rglob("*.c"):
        content = c_file.read_text(errors='ignore')

        # Split into functions (rough heuristic)
        # Look for function-like blocks
        func_blocks = re.split(r'\n\w+\s+\w+\s*\([^)]*\)\s*\{', content)

        for block in func_blocks:
            # Find all field accesses in this block
            pattern = rf'(\w+)->(\w+)'
            fields_in_block = set()

            for match in re.finditer(pattern, block):
                base_var = match.group(1)
                field = match.group(2)
                base_type = infer_base_type(base_var, content)

                if base_type and base_type.lower() == struct_type.lower():
                    # Only track unknown fields
                    if re.match(r'x[0-9A-Fa-f]+', field):
                        fields_in_block.add(field)

            # Track co-occurrences
            fields_list = list(fields_in_block)
            for i, f1 in enumerate(fields_list):
                for f2 in fields_list[i+1:]:
                    field_cooccurrence[f1][f2] += 1
                    field_cooccurrence[f2][f1] += 1

    # Find clusters (fields that frequently appear together)
    print(f"\nField clusters (commonly used together):\n")

    # Sort fields by how many co-occurrences they have
    field_scores = {}
    for f1, co in field_cooccurrence.items():
        field_scores[f1] = sum(co.values())

    seen = set()
    clusters = []

    for field in sorted(field_scores, key=lambda x: -field_scores[x]):
        if field in seen:
            continue

        # Build cluster starting from this field
        cluster = {field}
        queue = [field]

        while queue:
            current = queue.pop(0)
            for neighbor, count in field_cooccurrence[current].items():
                if neighbor not in seen and count >= 3:  # At least 3 co-occurrences
                    cluster.add(neighbor)
                    seen.add(neighbor)
                    queue.append(neighbor)

        if len(cluster) >= 2:
            # Sort cluster by offset
            def offset_key(f):
                m = re.match(r'x([0-9A-Fa-f]+)', f)
                return int(m.group(1), 16) if m else 0
            cluster = sorted(cluster, key=offset_key)
            clusters.append(cluster)
            seen.update(cluster)

    for i, cluster in enumerate(clusters[:20], 1):
        # Check if offsets are contiguous (suggests embedded struct)
        offsets = []
        for f in cluster:
            m = re.match(r'x([0-9A-Fa-f]+)', f)
            if m:
                offsets.append(int(m.group(1), 16))

        offsets.sort()
        if len(offsets) >= 2:
            span = offsets[-1] - offsets[0]
            density = len(offsets) / (span / 4 + 1)  # Assuming 4-byte fields

            contiguous = "contiguous" if density > 0.5 else "sparse"
            print(f"Cluster {i} (0x{offsets[0]:X}-0x{offsets[-1]:X}, {contiguous}):")
            for f in cluster:
                print(f"  {f}")
            print()


def cmd_template(func_name: str):
    """
    Show a template based on the most similar matched function.

    This helps you start decompiling by showing reference code from similar functions.
    """
    import subprocess

    print(f"Finding template for {func_name}...", file=sys.stderr)

    # Get similar decompiled functions
    result = subprocess.run(
        ['python3', str(Path(__file__).parent / 'tools.py'), 'similar', func_name, '--decompiled'],
        capture_output=True, text=True
    )

    if result.returncode != 0 or not result.stdout:
        print(f"Could not find similar functions for {func_name}", file=sys.stderr)
        return

    # Parse the output to get the most similar function
    lines = result.stdout.strip().split('\n')
    similar_func = None
    similarity = 0

    for line in lines:
        # Look for lines like " 0.995     DONE | ftCo_800AF78C"
        match = re.match(r'\s*(\d+\.\d+)\s+DONE\s+\|\s+(\w+)', line)
        if match:
            similarity = float(match.group(1))
            similar_func = match.group(2)
            break

    if not similar_func:
        print("No similar decompiled function found.", file=sys.stderr)
        return

    print(f"\nMost similar matched function: {similar_func} ({similarity:.1%} similar)")
    print("=" * 70)

    # Get the source file for the similar function
    result = subprocess.run(
        ['python3', str(Path(__file__).parent / 'tools.py'), 'asm', similar_func],
        capture_output=True, text=True
    )
    # The asm command outputs unit info that we can use to find the source

    # Search for the function in source files
    for c_file in MELEE_SRC.rglob("*.c"):
        content = c_file.read_text(errors='ignore')
        # Look for the function definition
        pattern = rf'^\s*(?:static\s+)?(?:inline\s+)?\w+[\s\*]+{re.escape(similar_func)}\s*\([^)]*\)\s*\{{'
        match = re.search(pattern, content, re.MULTILINE)
        if match:
            # Found it - extract the function body
            start = match.start()
            # Find the end of the function (matching braces)
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

            # Now show the target assembly for comparison
            print(f"\nTarget assembly for {func_name}:")
            print("-" * 70)
            result = subprocess.run(
                ['python3', str(Path(__file__).parent / 'tools.py'), 'asm', func_name],
                capture_output=True, text=True
            )
            # Only show first 50 lines
            asm_lines = result.stdout.strip().split('\n')[:50]
            print('\n'.join(asm_lines))
            if len(result.stdout.strip().split('\n')) > 50:
                print("... (truncated)")
            return

    print(f"Could not find source for {similar_func}", file=sys.stderr)


def cmd_suggest(func_name: str):
    """Suggest types for fields used in an unmatched function based on similar matched functions."""
    # This would integrate with the similarity search
    print(f"Analyzing {func_name} for type suggestions...", file=sys.stderr)

    # Get the assembly for this function
    import subprocess
    result = subprocess.run(
        ['python3', str(Path(__file__).parent / 'tools.py'), 'asm', func_name],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Could not get assembly for {func_name}", file=sys.stderr)
        return

    asm = result.stdout

    # Extract offset patterns from assembly
    # lwz rN, OFFSET(rBase) -> load from offset
    # stw rN, OFFSET(rBase) -> store to offset
    offset_pattern = r'(?:lwz|stw|lfs|stfs|lbz|stb|lhz|sth)\s+r\d+,\s*(\d+)\(r(\d+)\)'

    offsets_used = defaultdict(list)
    for match in re.finditer(offset_pattern, asm):
        offset = int(match.group(1))
        reg = match.group(2)
        instr = match.group(0).split()[0]
        offsets_used[offset].append(instr)

    print(f"\nOffsets accessed in {func_name}:\n")
    print(f"{'Offset':>8} | {'Instructions':30} | Likely Type")
    print(f"{'-'*8} | {'-'*30} | {'-'*15}")

    for offset in sorted(offsets_used.keys()):
        instrs = offsets_used[offset]
        instr_str = ', '.join(set(instrs))

        # Infer type from instruction
        likely_type = "unknown"
        if any(i in ['lfs', 'stfs'] for i in instrs):
            likely_type = "float"
        elif any(i in ['lbz', 'stb'] for i in instrs):
            likely_type = "u8/s8"
        elif any(i in ['lhz', 'sth'] for i in instrs):
            likely_type = "u16/s16"
        elif any(i in ['lwz', 'stw'] for i in instrs):
            likely_type = "u32/s32/ptr"

        print(f"  0x{offset:04X} | {instr_str:30} | {likely_type}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Type inference for Melee decomp")
    subparsers = parser.add_subparsers(dest='command')

    # infer command
    infer_parser = subparsers.add_parser('infer', help='Infer types for unknown fields')
    infer_parser.add_argument('--struct', '-s', help='Filter to specific struct')
    infer_parser.add_argument('--verbose', '-v', action='store_true')

    # usage command
    usage_parser = subparsers.add_parser('usage', help='Show usages of a field')
    usage_parser.add_argument('struct_type', help='Struct type (e.g., Fighter)')
    usage_parser.add_argument('field_name', help='Field name (e.g., x2C)')

    # patterns command
    patterns_parser = subparsers.add_parser('patterns', help='Find field co-occurrence patterns')
    patterns_parser.add_argument('--struct', '-s', default='Fighter', help='Struct to analyze')

    # suggest command
    suggest_parser = subparsers.add_parser('suggest', help='Suggest types for a function')
    suggest_parser.add_argument('func_name', help='Function name')

    # template command
    template_parser = subparsers.add_parser('template', help='Show template from similar matched function')
    template_parser.add_argument('func_name', help='Function name to find template for')

    args = parser.parse_args()

    if args.command == 'infer':
        cmd_infer(args.struct if hasattr(args, 'struct') else None,
                  args.verbose if hasattr(args, 'verbose') else False)
    elif args.command == 'usage':
        cmd_usage(args.struct_type, args.field_name)
    elif args.command == 'patterns':
        cmd_patterns(args.struct if hasattr(args, 'struct') else 'Fighter')
    elif args.command == 'suggest':
        cmd_suggest(args.func_name)
    elif args.command == 'template':
        cmd_template(args.func_name)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
