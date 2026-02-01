#!/usr/bin/env python3
"""
Code quality checker for Melee decompilation matches.

Detects patterns that indicate low-quality or "fake" matches:
- Pointer arithmetic with casts instead of proper struct access
- Raw array access with magic numbers instead of structs
- Generic parameter names (arg0, arg1)
- Missing documentation on non-trivial functions

Adapted from Chris Lewis's detect_low_quality_matches.py for the
GC/mwcc codebase conventions used in the Melee decomp.
"""

import re
import sys
from pathlib import Path


class QualityIssue:
    """A single quality issue found in decompiled code."""

    def __init__(self, severity: str, category: str, message: str,
                 line_num: int = 0, line_text: str = ""):
        self.severity = severity  # "error", "warning", "info"
        self.category = category
        self.message = message
        self.line_num = line_num
        self.line_text = line_text

    def __str__(self):
        loc = f":{self.line_num}" if self.line_num else ""
        return f"[{self.severity.upper()}] {self.category}{loc}: {self.message}"


def check_pointer_arithmetic(code: str) -> list[QualityIssue]:
    """Detect pointer arithmetic with casts instead of struct field access.

    Patterns like: *(type*)((u8*)ptr + offset)
    These should use proper struct definitions instead.
    """
    issues = []

    # Pattern: *(type*)((u8/s8/char*)ptr + offset)
    pattern = r'\*\s*\(\s*\w+\s*\*\s*\)\s*\(\s*\(\s*(?:u8|s8|char)\s*\*\s*\)\s*\w+\s*[+-]\s*(?:0x[\dA-Fa-f]+|\d+)\s*\)'
    for i, line in enumerate(code.split('\n'), 1):
        if re.search(pattern, line):
            issues.append(QualityIssue(
                "error", "pointer-arithmetic",
                "Pointer arithmetic with cast instead of struct field access",
                line_num=i, line_text=line.strip()
            ))

    # Pattern: ((type*)ptr)[offset] - casting pointer then indexing
    pattern2 = r'\(\s*\(\s*\w+\s*\*\s*\)\s*\w+\s*\)\s*\[\s*(?:0x[\dA-Fa-f]+|\d+)\s*\]'
    for i, line in enumerate(code.split('\n'), 1):
        if re.search(pattern2, line):
            issues.append(QualityIssue(
                "warning", "pointer-arithmetic",
                "Cast-and-index pattern - consider using struct fields",
                line_num=i, line_text=line.strip()
            ))

    return issues


def check_raw_array_access(code: str) -> list[QualityIssue]:
    """Detect raw array access with magic number arithmetic.

    Patterns like: data[x * 5 + y][0]
    These should use proper struct types with named fields.
    """
    issues = []

    # Pattern: array[expr * N + expr] - magic number multiplication in index
    pattern = r'\w+\s*\[\s*\w+\s*\*\s*\d+\s*[+-]\s*\w+\s*\]'
    for i, line in enumerate(code.split('\n'), 1):
        if re.search(pattern, line):
            # Skip if it's a comment
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('/*'):
                continue
            issues.append(QualityIssue(
                "warning", "raw-array",
                "Array access with magic number arithmetic - consider using struct",
                line_num=i, line_text=stripped
            ))

    return issues


def check_generic_names(code: str) -> list[QualityIssue]:
    """Detect generic parameter and variable names.

    Names like arg0, arg1, var1, x0, x2 suggest the decompiler didn't
    understand what the code does.
    """
    issues = []

    # Check function parameters for generic names
    func_pattern = r'^\s*\w[\w\s\*]*\s+\w+\s*\(([^)]+)\)'
    for match in re.finditer(func_pattern, code, re.MULTILINE):
        params = match.group(1)
        for param in params.split(','):
            param = param.strip()
            # Extract parameter name (last word)
            words = param.split()
            if not words:
                continue
            name = words[-1].lstrip('*')

            if re.match(r'^(arg|var|param)\d+$', name):
                issues.append(QualityIssue(
                    "warning", "generic-name",
                    f"Generic parameter name '{name}' - use descriptive name",
                    line_text=param.strip()
                ))

    # Check for m2c-style variable names in declarations
    m2c_pattern = r'\b(?:temp_[rf]\d+|sp[A-F0-9]+|var_[rf]\d+)\b'
    for i, line in enumerate(code.split('\n'), 1):
        if re.search(m2c_pattern, line):
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('/*'):
                continue
            issues.append(QualityIssue(
                "error", "m2c-artifact",
                "m2c-generated variable name left in code",
                line_num=i, line_text=stripped
            ))

    return issues


def check_type_punning(code: str) -> list[QualityIssue]:
    """Detect type punning patterns that indicate fake matches.

    Patterns like: *(s32*)&float_field
    These force specific load instructions but aren't what HAL wrote.
    """
    issues = []

    # Pattern: *(type*)&var - type punning via address-of
    pattern = r'\*\s*\(\s*(?:s32|u32|int|s16|u16|f32|f64)\s*\*\s*\)\s*&'
    for i, line in enumerate(code.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('/*'):
            continue
        if 'FAKE MATCH' in line or 'FAKE_MATCH' in line:
            continue  # Already documented
        if re.search(pattern, line):
            issues.append(QualityIssue(
                "error", "type-punning",
                "Type punning via address-of - likely fake match pattern",
                line_num=i, line_text=stripped
            ))

    return issues


def check_redundant_casts(code: str) -> list[QualityIssue]:
    """Detect redundant or suspicious cast chains."""
    issues = []

    # Pattern: (type1)(type2)(type3)expr - triple cast chain
    pattern = r'\(\s*\w+\s*\)\s*\(\s*\w+\s*\)\s*\(\s*\w+\s*\)'
    for i, line in enumerate(code.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('/*'):
            continue
        if re.search(pattern, line):
            issues.append(QualityIssue(
                "warning", "redundant-cast",
                "Chain of 3+ casts - likely a fake match pattern",
                line_num=i, line_text=stripped
            ))

    return issues


def check_documentation(code: str, func_name: str) -> list[QualityIssue]:
    """Check for missing documentation on non-trivial functions.

    Trivial functions (< 5 lines) don't need docs.
    """
    issues = []

    # Find the function body
    pattern = rf'\w[\w\s\*]*\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{'
    match = re.search(pattern, code)
    if not match:
        return issues

    # Count lines in function body
    start = match.start()
    brace_count = 0
    in_func = False
    end = start
    for i, char in enumerate(code[start:], start):
        if char == '{':
            brace_count += 1
            in_func = True
        elif char == '}':
            brace_count -= 1
            if in_func and brace_count == 0:
                end = i + 1
                break

    func_body = code[start:end]
    line_count = func_body.count('\n')

    if line_count < 5:
        return issues  # Trivial function, no docs needed

    # Check for any comment before the function
    pre_func = code[:start].rstrip()
    last_lines = pre_func.split('\n')[-3:]
    has_comment = any('//' in line or '/*' in line or '*/' in line
                      for line in last_lines)

    if not has_comment:
        issues.append(QualityIssue(
            "info", "missing-docs",
            f"Non-trivial function ({line_count} lines) has no documentation"
        ))

    return issues


def check_melee_style(code: str) -> list[QualityIssue]:
    """Check for Melee-specific style violations.

    Based on CONTRIBUTING.md guidelines.
    """
    issues = []

    for i, line in enumerate(code.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('/*'):
            continue

        # Check float literal style: should be 1.0F not 1.0f
        if re.search(r'\d+\.\d+f\b', stripped):
            issues.append(QualityIssue(
                "warning", "style-float",
                "Use uppercase F for float literals (1.0F not 1.0f)",
                line_num=i, line_text=stripped
            ))

        # Check for !ptr instead of ptr == NULL
        if re.search(r'if\s*\(\s*!(?!(?:=))\w+\s*\)', stripped):
            # This is tricky - !var could be a bool check which is fine
            # Only flag if the variable looks like a pointer (ends with p, ptr, obj, etc.)
            match = re.search(r'if\s*\(\s*!(\w+)\s*\)', stripped)
            if match:
                var = match.group(1)
                if var.endswith(('_p', 'ptr', 'obj', 'gobj', 'jobj', 'fp')):
                    issues.append(QualityIssue(
                        "info", "style-null-check",
                        f"Use explicit NULL check: '{var} == NULL' instead of '!{var}'",
                        line_num=i, line_text=stripped
                    ))

    return issues


def check_local_struct_definitions(code: str) -> list[QualityIssue]:
    """Detect local struct definitions that should be in headers.

    Patterns like: typedef struct itFoo_ItemVars { ... } itFoo_ItemVars;
    These should be defined in itCommonItems.h or the appropriate header.
    """
    issues = []

    # Pattern: typedef struct XxxVars/Attrs/Attributes at file scope
    pattern = r'^typedef\s+struct\s+(\w+(?:_ItemVars|_GroundVars|Attributes|Attrs))\s*\{'
    for match in re.finditer(pattern, code, re.MULTILINE):
        struct_name = match.group(1)
        line_num = code[:match.start()].count('\n') + 1
        issues.append(QualityIssue(
            "warning", "local-struct",
            f"Local struct '{struct_name}' should be defined in a header file "
            "(e.g., itCommonItems.h) and added to the union if applicable",
            line_num=line_num
        ))

    return issues


def check_wrong_union_variant(code: str, file_path: str = "") -> list[QualityIssue]:
    """Detect potential wrong union variant usage.

    When working in grbigblue.c, using gv.corneria is likely wrong.
    When working in itfoo.c, using xDD4_itemVar.bar (where bar != foo) is suspect.
    """
    issues = []

    if not file_path:
        return issues

    # Extract the base name (e.g., "bigblue" from "grbigblue.c")
    import os
    basename = os.path.basename(file_path).replace('.c', '').lower()

    # Ground files: check gv.variant usage
    if basename.startswith('gr'):
        stage_name = basename[2:]  # Remove "gr" prefix
        # Look for gv.something patterns
        gv_pattern = r'gv\.(\w+)\.'
        for match in re.finditer(gv_pattern, code):
            variant = match.group(1).lower()
            # If the variant doesn't match the stage name, it might be wrong
            if variant != stage_name and variant not in ('map', stage_name + 'route'):
                line_num = code[:match.start()].count('\n') + 1
                issues.append(QualityIssue(
                    "warning", "wrong-union-variant",
                    f"Using 'gv.{match.group(1)}' in {basename}.c - expected 'gv.{stage_name}' or similar",
                    line_num=line_num
                ))

    # Item files: check xDD4_itemVar.variant usage
    if basename.startswith('it'):
        item_name = basename[2:]  # Remove "it" prefix
        # Look for xDD4_itemVar.something patterns
        var_pattern = r'xDD4_itemVar\.(\w+)\.'
        for match in re.finditer(var_pattern, code):
            variant = match.group(1).lower()
            # Common false positives to skip
            if variant in ('_', 'pad'):
                continue
            # If the variant doesn't match the item name, flag it
            if variant != item_name:
                line_num = code[:match.start()].count('\n') + 1
                issues.append(QualityIssue(
                    "info", "union-variant-mismatch",
                    f"Using 'xDD4_itemVar.{match.group(1)}' in {basename}.c - "
                    f"expected 'xDD4_itemVar.{item_name}' (verify this is correct)",
                    line_num=line_num
                ))

    return issues


def check_raw_offset_access(code: str) -> list[QualityIssue]:
    """Detect raw offset access patterns that indicate missing struct fields.

    Patterns like: *(s32*)((u8*)&ip->xDD4_itemVar + 0x50)
    These should use proper struct fields instead.
    """
    issues = []

    # Pattern: cast-and-offset into a struct
    pattern = r'\*\s*\(\s*\w+\s*\*\s*\)\s*\(\s*\(\s*u8\s*\*\s*\)\s*&?\w+->[\w.]+\s*\+\s*0x[0-9A-Fa-f]+\s*\)'
    for match in re.finditer(pattern, code):
        line_num = code[:match.start()].count('\n') + 1
        issues.append(QualityIssue(
            "warning", "raw-offset-access",
            "Raw offset access into struct - add the missing field to the struct definition",
            line_num=line_num, line_text=match.group(0)[:60]
        ))

    return issues


def check_sandbox_comments(code: str) -> list[QualityIssue]:
    """Detect sandbox metadata comments that shouldn't be in production code.

    These are generated by m2c/vacuum sandboxes and should be stripped during integration.
    """
    issues = []

    for i, line in enumerate(code.split('\n'), 1):
        stripped = line.strip()
        # Check for "// Decompilation of X" comments
        if re.match(r'^//\s*Decompilation of\s+\w+', stripped):
            issues.append(QualityIssue(
                "error", "sandbox-comment",
                "Sandbox comment '// Decompilation of' should be removed",
                line_num=i, line_text=stripped
            ))
        # Check for "// Unit: X" comments
        elif re.match(r'^//\s*Unit:\s*\S+', stripped):
            issues.append(QualityIssue(
                "error", "sandbox-comment",
                "Sandbox comment '// Unit:' should be removed",
                line_num=i, line_text=stripped
            ))
        # Check for "// m2c decompilation of X" comments
        elif re.match(r'^//\s*m2c decompilation of\s+\w+', stripped):
            issues.append(QualityIssue(
                "error", "sandbox-comment",
                "Sandbox comment '// m2c decompilation of' should be removed",
                line_num=i, line_text=stripped
            ))

    return issues


def check_goto_patterns(code: str) -> list[QualityIssue]:
    """Detect goto patterns that should use structured control flow.

    - `goto next_iter` at the end of a loop should be `continue`
    - Labels used for boolean checks could be inline functions
    """
    issues = []
    lines = code.split('\n')

    # Track if we're in a loop context
    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Detect `goto next_iter;` or similar end-of-loop patterns
        if re.match(r'^goto\s+(?:next_iter|loop_end|continue_loop|next)\s*;', stripped):
            issues.append(QualityIssue(
                "warning", "goto-continue",
                "Use 'continue' instead of 'goto next_iter' inside loops",
                line_num=i, line_text=stripped
            ))

        # Detect labels that look like loop continuation points
        if re.match(r'^(next_iter|loop_end|continue_loop|next)\s*:', stripped):
            # Check if this is followed by loop increment/update
            for j in range(i, min(i + 5, len(lines) + 1)):
                next_line = lines[j - 1].strip() if j <= len(lines) else ""
                if re.match(r'^\}\s*while\s*\(', next_line) or \
                   re.search(r'\+\+|--|\+=|-=', next_line):
                    issues.append(QualityIssue(
                        "info", "goto-continue",
                        "This label pattern could use 'continue' in a proper for/while loop",
                        line_num=i, line_text=stripped
                    ))
                    break

        # Detect label-and-goto patterns that could be inline bool functions
        # Pattern: variable = 0/1; goto label; ... label: if (variable)
        if re.match(r'^(?:skip|found|result|flag)\s*=\s*[01]\s*;', stripped):
            # Look for a following goto
            if i < len(lines):
                next_stripped = lines[i].strip()
                if re.match(r'^goto\s+\w+\s*;', next_stripped):
                    issues.append(QualityIssue(
                        "info", "goto-inline",
                        "This skip/flag + goto pattern could be an inline bool function",
                        line_num=i, line_text=stripped
                    ))

    return issues


def check_midfile_typedefs(code: str) -> list[QualityIssue]:
    """Detect typedef definitions that appear mid-file instead of at top.

    Typedefs should be at the top of the file or in header files.
    Only flags typedefs that appear AFTER the first function definition.
    """
    issues = []
    lines = code.split('\n')

    # Find the first function definition (not typedef, extern, static var, or stub)
    first_func_line = None
    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip header-section items
        if stripped == '' or stripped.startswith('//') or stripped.startswith('/*'):
            continue
        if stripped.startswith('#'):
            continue
        if stripped.startswith('extern ') or stripped.startswith('typedef '):
            continue
        if stripped.startswith('static ') and '(' not in stripped:
            continue  # Static variable, not function

        # Look for function definition pattern: return_type func_name(
        # Must have a brace-starting body or be multi-line
        if re.match(r'^(?:static\s+)?(?:inline\s+)?\w+[\s\*]+\w+\s*\([^)]*\)\s*\{?', stripped):
            # Check this isn't a forward declaration (no body)
            if '{' in stripped or (i < len(lines) and '{' in lines[i]):
                first_func_line = i
                break

    if first_func_line is None:
        return issues  # No functions found, all typedefs are fine

    # Now check for typedefs after the first function
    for i, line in enumerate(lines, 1):
        if i <= first_func_line:
            continue
        stripped = line.strip()
        if stripped.startswith('typedef '):
            # Function pointer typedefs are OK inline
            if '(*)' in stripped or '(*' in stripped:
                continue
            issues.append(QualityIssue(
                "warning", "midfile-typedef",
                "Move typedef to top of file or to appropriate header",
                line_num=i, line_text=stripped[:80]
            ))

    return issues


def check_function(code: str, func_name: str = "", file_path: str = "") -> list[QualityIssue]:
    """Run all quality checks on a function's code.

    Returns a list of QualityIssue objects sorted by severity.
    """
    issues = []

    issues.extend(check_pointer_arithmetic(code))
    issues.extend(check_raw_array_access(code))
    issues.extend(check_generic_names(code))
    issues.extend(check_type_punning(code))
    issues.extend(check_redundant_casts(code))
    issues.extend(check_melee_style(code))
    issues.extend(check_local_struct_definitions(code))
    issues.extend(check_wrong_union_variant(code, file_path))
    issues.extend(check_raw_offset_access(code))
    issues.extend(check_sandbox_comments(code))
    issues.extend(check_goto_patterns(code))

    if func_name:
        issues.extend(check_documentation(code, func_name))

    # Sort by severity: error > warning > info
    severity_order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda x: severity_order.get(x.severity, 3))

    return issues


def check_file(file_path: Path) -> dict[str, list[QualityIssue]]:
    """Run quality checks on all functions in a C source file.

    Returns dict mapping function names to their issues.
    """
    if not file_path.exists():
        return {}

    content = file_path.read_text(errors='ignore')
    results = {}

    # File-level checks (not per-function)
    file_issues = []
    file_issues.extend(check_sandbox_comments(content))
    file_issues.extend(check_midfile_typedefs(content))
    if file_issues:
        results["(file-level)"] = file_issues

    # Find all function definitions
    func_pattern = r'^\s*(?:static\s+)?(?:inline\s+)?\w[\w\s\*]*\s+(\w+)\s*\([^)]*\)\s*\{'
    for match in re.finditer(func_pattern, content, re.MULTILINE):
        func_name = match.group(1)

        # Skip common non-function matches
        if func_name in ('if', 'for', 'while', 'switch', 'return'):
            continue

        # Extract function body
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
        issues = check_function(func_code, func_name, str(file_path))

        if issues:
            results[func_name] = issues

    return results


def main():
    """CLI entry point for quality checking."""
    if len(sys.argv) < 2:
        print("Usage: quality_check.py <function_or_file> [--strict]")
        print()
        print("Check code quality for decompiled functions.")
        print()
        print("Arguments:")
        print("  <function_or_file>  Function name or .c file path")
        print("  --strict            Treat warnings as errors")
        print()
        print("Exit codes:")
        print("  0  No errors found")
        print("  1  Errors found (or warnings in --strict mode)")
        sys.exit(1)

    target = sys.argv[1]
    strict = "--strict" in sys.argv

    target_path = Path(target)

    if target_path.exists() and target_path.suffix == '.c':
        # Check entire file
        results = check_file(target_path)

        if not results:
            print(f"No quality issues found in {target_path}")
            sys.exit(0)

        has_errors = False
        for func_name, issues in results.items():
            print(f"\n{func_name}:")
            for issue in issues:
                print(f"  {issue}")
                if issue.line_text:
                    print(f"    > {issue.line_text}")
                if issue.severity == "error" or (strict and issue.severity == "warning"):
                    has_errors = True

        sys.exit(1 if has_errors else 0)

    else:
        # Treat as function code from stdin or look in sandbox
        code = sys.stdin.read() if not sys.stdin.isatty() else ""

        if not code:
            # Try to find in sandbox
            sandbox_dir = Path(__file__).parent / "sandboxes" / target
            if sandbox_dir.exists():
                # Find best base_N.c
                bases = sorted(sandbox_dir.glob("base_*.c"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                if bases:
                    code = bases[0].read_text()
                else:
                    base = sandbox_dir / "base.c"
                    if base.exists():
                        code = base.read_text()

            if not code:
                print(f"No code found for '{target}'", file=sys.stderr)
                sys.exit(1)

        issues = check_function(code, target)

        if not issues:
            print(f"No quality issues found for {target}")
            sys.exit(0)

        has_errors = False
        for issue in issues:
            print(f"  {issue}")
            if issue.line_text:
                print(f"    > {issue.line_text}")
            if issue.severity == "error" or (strict and issue.severity == "warning"):
                has_errors = True

        error_count = sum(1 for i in issues if i.severity == "error")
        warn_count = sum(1 for i in issues if i.severity == "warning")
        info_count = sum(1 for i in issues if i.severity == "info")

        print(f"\nSummary: {error_count} errors, {warn_count} warnings, {info_count} info")

        sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
