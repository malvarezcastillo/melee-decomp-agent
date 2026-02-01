#!/usr/bin/env python3
"""
Autonomous decompilation vacuum - processes easy functions in a loop.

Inspired by Chris Lewis's Snowboard Kids 2 decomp vacuum system.
Picks the easiest undecompiled functions (high similarity to matched code),
creates isolated sandboxes, invokes Claude CLI to attempt decompilation,
and integrates successful matches into the source tree.

Usage:
    python3 vacuum.py [--max N] [--branch NAME] [--timeout SECS] [--dry-run]

Options:
    --max N         Maximum number of functions to attempt (default: unlimited)
    --branch NAME   Git branch name (default: vacuum/YYYYMMDD-HHMMSS)
    --timeout SECS  Timeout per function attempt in seconds (default: 600)
    --dry-run       Pick functions and create sandboxes but don't invoke Claude
    --no-commit     Skip git commits after successful matches
    --worktree DIR  Use existing worktree directory (default: create new)
"""

import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MELEE_AI = Path(__file__).parent
TOOLS_PY = MELEE_AI / "tools.py"
SANDBOXES_DIR = MELEE_AI / "sandboxes"
DIFFICULT_FUNCTIONS = MELEE_AI / ".difficult_functions"
SUBMITTED_PRS = MELEE_AI / ".submitted_prs"
VACUUM_LOGS_DIR = MELEE_AI / "vacuum_logs"


def get_lock_file(melee_repo: str) -> Path:
    """Get the lock file path for a specific worktree."""
    # Use hash of repo path to create unique lock file
    repo_hash = hashlib.md5(melee_repo.encode()).hexdigest()[:8]
    return MELEE_AI / f".vacuum-{repo_hash}.lock"


def check_vacuum_lock(melee_repo: str) -> tuple[bool, str]:
    """Check if another vacuum is running on this worktree.

    Returns (is_locked, message).
    """
    lock_file = get_lock_file(melee_repo)
    if not lock_file.exists():
        return False, ""

    try:
        lock_data = lock_file.read_text().strip().split('\n')
        pid = int(lock_data[0])
        info = lock_data[1] if len(lock_data) > 1 else "unknown"

        # Check if process is still running
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            return True, f"Vacuum already running on {melee_repo} (PID {pid}): {info}"
        except OSError:
            # Process not running, stale lock
            lock_file.unlink()
            return False, ""
    except (ValueError, IndexError):
        # Malformed lock file
        lock_file.unlink()
        return False, ""


def acquire_vacuum_lock(melee_repo: str, info: str) -> bool:
    """Try to acquire the vacuum lock for a worktree.

    Returns True if lock acquired, False if another vacuum is running.
    """
    is_locked, msg = check_vacuum_lock(melee_repo)
    if is_locked:
        print(f"ERROR: {msg}", file=sys.stderr)
        print("Kill the other vacuum first or wait for it to finish.", file=sys.stderr)
        return False

    # Write lock file
    lock_file = get_lock_file(melee_repo)
    lock_file.write_text(f"{os.getpid()}\n{info}\n")
    return True


def release_vacuum_lock(melee_repo: str):
    """Release the vacuum lock for a worktree."""
    try:
        lock_file = get_lock_file(melee_repo)
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass


class Vacuum:
    """Autonomous decompilation loop driver."""

    def __init__(self, max_iterations: int | None = None,
                 branch_name: str | None = None,
                 timeout: int = 600,
                 dry_run: bool = False,
                 no_commit: bool = False,
                 melee_repo: str | None = None,
                 min_similarity: float | None = None,
                 max_instructions: int | None = None,
                 unit_filter: str | None = None):
        self.max_iterations = max_iterations
        self.branch_name = branch_name or f"vacuum/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.timeout = timeout
        self.dry_run = dry_run
        self.no_commit = no_commit
        self.melee_repo = melee_repo or os.environ.get("MELEE_REPO", "")
        self.min_similarity = min_similarity
        self.max_instructions = max_instructions
        self.unit_filter = unit_filter

        self.matched: list[str] = []
        self.failed: list[str] = []

        # Session-specific log file
        VACUUM_LOGS_DIR.mkdir(exist_ok=True)
        session_id = datetime.now().strftime('%Y%m%d-%H%M%S')
        if unit_filter:
            session_id += f"-{unit_filter.replace('/', '_')}"
        self.log_file = VACUUM_LOGS_DIR / f"vacuum-{session_id}.log"
        self.attempted: set[str] = set()  # All functions tried this session
        self.running = True
        self.current_func: str | None = None

        # Set up Ctrl-C handler
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle Ctrl-C gracefully - finish current attempt then stop."""
        if self.current_func:
            self.log(f"Interrupt received. Finishing current attempt ({self.current_func})...")
            self.running = False
        else:
            self.log("Interrupt received. Stopping.")
            self.running = False

    def log(self, message: str):
        """Log a message to both stdout and session log file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        with open(self.log_file, "a") as f:
            f.write(line + "\n")

    def pick_function(self) -> str | None:
        """Get the next best candidate from vacuum-pick."""
        env = os.environ.copy()
        if self.melee_repo:
            env["MELEE_REPO"] = self.melee_repo

        # Request extra candidates in case some were already attempted this session
        request_n = max(10, len(self.attempted) + 5)
        cmd = [sys.executable, str(TOOLS_PY), "vacuum-pick", str(request_n)]
        if self.min_similarity is not None:
            cmd.extend(["--min-similarity", str(self.min_similarity)])
        if self.max_instructions is not None:
            cmd.extend(["--max-instructions", str(self.max_instructions)])
        if self.unit_filter is not None:
            cmd.extend(["--unit", self.unit_filter])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=300, env=env
            )
        except subprocess.TimeoutExpired:
            self.log("vacuum-pick timed out")
            return None

        if result.returncode != 0:
            self.log(f"vacuum-pick failed: {result.stderr.strip()}")
            return None

        # Parse output - look for score lines like "  0.958 0.975  0.03 | mnGallery_80259604"
        # Skip headers, sub-lines (└─), empty lines, and already-attempted functions
        for line in result.stdout.strip().split('\n'):
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith('-'):
                continue
            if '└' in stripped or 'VScore' in stripped:
                continue
            # Score line: numbers | function_name
            if '|' in stripped:
                after_pipe = stripped.split('|', 1)[1].strip()
                if after_pipe and not after_pipe.startswith('-'):
                    func_name = after_pipe.split()[0]
                    if func_name not in self.attempted:
                        return func_name

        return None

    def create_sandbox(self, func_name: str) -> bool:
        """Create a sandbox for the function."""
        env = os.environ.copy()
        if self.melee_repo:
            env["MELEE_REPO"] = self.melee_repo

        try:
            result = subprocess.run(
                [sys.executable, str(TOOLS_PY), "sandbox", func_name],
                capture_output=True, text=True, timeout=120, env=env
            )
        except subprocess.TimeoutExpired:
            self.log(f"  sandbox creation timed out for {func_name}")
            return False

        if result.returncode != 0:
            self.log(f"  sandbox failed: {result.stderr.strip()}")
            return False

        return True

    def attempt_function_in_sandbox(self, func_name: str) -> float:
        """Invoke Claude CLI one-shot in an existing sandbox. Returns best match %."""
        sandbox_dir = SANDBOXES_DIR / func_name

        if self.dry_run:
            self.log(f"  [dry-run] Would invoke Claude CLI in {sandbox_dir}")
            return 0.0

        # Invoke claude CLI one-shot
        prompt = (
            f"Decompile the function {func_name}. "
            f"Follow the instructions in CLAUDE.md. "
            f"Read template.c for reference patterns and target.s for the target assembly. "
            f"Create successive base_N.c files and test with ./build.sh base_N.c. "
            f"Aim for 100% match. If stuck, try fundamentally different approaches "
            f"(different loop types, reordered declarations, alternative casts). "
            f"IMPORTANT: Before stopping, always ensure your best compiling version "
            f"has been submitted through ./build.sh. Never quit without at least one "
            f"scored attempt. Your partial work will be reviewed and built upon."
        )

        try:
            result = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", prompt],
                cwd=sandbox_dir,
                capture_output=True, text=True,
                timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            self.log(f"  Claude CLI timed out after {self.timeout}s")
            return 0.0
        except FileNotFoundError:
            self.log("  ERROR: 'claude' CLI not found. Install Claude Code first.")
            self.running = False
            return 0.0

        # Check match_log.txt for results
        return self.check_match(sandbox_dir, func_name)

    def check_match(self, sandbox_dir: Path, func_name: str) -> float:
        """Check match quality by reading match_log.txt. Returns best percentage."""
        match_log = sandbox_dir / "match_log.txt"
        if not match_log.exists():
            self.log(f"  No match_log.txt found")
            return 0.0

        lines = match_log.read_text().strip().split('\n')
        if not lines:
            return 0.0

        best_pct = 0.0
        best_file = ""
        for line in lines:
            # Format: "base_N.c XX.XX%"
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    pct = float(parts[1].rstrip('%'))
                    if pct > best_pct:
                        best_pct = pct
                        best_file = parts[0]
                except ValueError:
                    continue

        self.log(f"  Best match: {best_pct:.2f}% ({best_file})")

        return best_pct

    def get_best_matched_file(self, sandbox_dir: Path) -> tuple[str, float]:
        """Get the best matched file name and percentage from match_log.txt."""
        match_log = sandbox_dir / "match_log.txt"
        if not match_log.exists():
            return "", 0.0

        best_pct = 0.0
        best_file = ""
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
        return best_file, best_pct

    def quality_review_in_sandbox(self, func_name: str) -> list[str]:
        """Run quality check and invoke Claude to fix issues or add comments.

        Returns list of issues that were found (empty if none).
        This is a best-effort pass - we commit regardless of outcome.
        """
        sandbox_dir = SANDBOXES_DIR / func_name
        best_file, best_pct = self.get_best_matched_file(sandbox_dir)

        if not best_file or best_pct < 100.0:
            return []  # Only do quality review on 100% matches

        matched_file = sandbox_dir / best_file
        if not matched_file.exists():
            return []

        # Run quality check on the matched file
        try:
            result = subprocess.run(
                [sys.executable, str(MELEE_AI / "quality_check.py"), str(matched_file)],
                capture_output=True, text=True, timeout=30
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

        # Parse quality issues from output
        issues = []
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if line and ('[WARNING]' in line or '[INFO]' in line or '[ERROR]' in line):
                issues.append(line)

        if not issues:
            self.log(f"  Quality check passed - no issues found")
            return []

        self.log(f"  Quality check found {len(issues)} issue(s), running review pass...")
        for issue in issues[:3]:  # Log first 3
            self.log(f"    {issue}")
        if len(issues) > 3:
            self.log(f"    ... and {len(issues) - 3} more")

        if self.dry_run:
            return issues

        # Invoke Claude to fix quality issues or add comments
        issues_text = "\n".join(issues)
        quality_prompt = (
            f"Your 100% matched code for {func_name} has quality issues:\n\n"
            f"{issues_text}\n\n"
            f"Read the 'Code Quality Rules' section in CLAUDE.md for guidance.\n\n"
            f"Please fix these issues while maintaining the 100% match. "
            f"Common fixes:\n"
            f"- 'wrong-union-variant': Use the correct union field for this file (e.g., xDD4_itemVar.{func_name.replace('it_', '').replace('it', '').split('_')[0].lower()} for item files)\n"
            f"- 'local-struct': Note in a comment that this struct should be moved to itCommonItems.h\n"
            f"- 'raw-offset-access': Note in a comment what struct field is needed\n"
            f"- 'type-punning': Note in a comment what the correct field type should be\n\n"
            f"If you CANNOT fix an issue without breaking the match, add a comment like:\n"
            f"/// TODO: <description of what needs to be fixed>\n\n"
            f"After making changes, run ./build.sh to verify you still have 100% match. "
            f"Create a new base_N.c with your quality improvements."
        )

        try:
            subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", quality_prompt],
                cwd=sandbox_dir,
                capture_output=True, text=True,
                timeout=180  # Shorter timeout for quality pass
            )
        except subprocess.TimeoutExpired:
            self.log(f"  Quality review timed out")
        except FileNotFoundError:
            pass

        # Check if we still have a 100% match after quality review
        new_best_file, new_pct = self.get_best_matched_file(sandbox_dir)
        if new_pct >= 100.0:
            self.log(f"  Quality review complete, still 100% match")
        elif new_pct >= best_pct:
            self.log(f"  Quality review complete, match maintained at {new_pct:.2f}%")
        else:
            self.log(f"  Quality review may have regressed match ({new_pct:.2f}%), using original")

        return issues

    def _fix_unk_params_from_build_errors(self, build_output: str, repo_dir: Path) -> bool:
        """Parse build errors for UNK_PARAMS mismatches and fix header prototypes.

        When decompiled code calls a function that still has UNK_PARAMS in its
        header prototype, mwcc rejects the call because UNK_PARAMS expands to
        'void' (meaning "no parameters"). This method parses the build errors,
        extracts the actual parameter types from the error messages, and updates
        the header prototypes.

        Returns True if any fixes were made (caller should retry build).
        """
        # Normalize mwcc output - strip "#   " comment prefixes from lines
        build_output = re.sub(r'^#\s*', '', build_output, flags=re.MULTILINE)

        # Pattern: function call 'func_name(params)' does not match
        pattern = r"function call '(\w+)\(([^)]*)\)' does not match"
        matches = re.findall(pattern, build_output)

        if not matches:
            return False

        fixed = False
        seen = set()
        for func_name, raw_params in matches:
            if func_name in seen:
                continue
            seen.add(func_name)

            # Convert "struct X *" to "X*" for typedef form
            clean_params = re.sub(r'struct (\w+) \*', r'\1*', raw_params)
            # Clean up extra spaces around commas
            clean_params = re.sub(r'\s*,\s*', ', ', clean_params.strip())

            # Find header with UNK_PARAMS prototype for this function
            for hpath in glob.glob(str(repo_dir / "src/**/*.h"), recursive=True):
                try:
                    htext = Path(hpath).read_text()
                except OSError:
                    continue

                # Match: /* ADDR */ RETTYPE func_name(UNK_PARAMS);
                proto_pat = (
                    rf'(/\*.*?\*/\s+)'          # addr comment
                    rf'(\S[\w\s\*]*?)\s+'        # return type
                    rf'{re.escape(func_name)}'   # function name
                    rf'\s*\(UNK_PARAMS\)\s*;'    # (UNK_PARAMS);
                )
                m = re.search(proto_pat, htext)
                if m:
                    old_proto = m.group(0)
                    addr_comment = m.group(1)
                    ret_type = m.group(2).strip()
                    new_proto = f"{addr_comment}{ret_type} {func_name}({clean_params});"
                    new_htext = htext.replace(old_proto, new_proto)
                    Path(hpath).write_text(new_htext)
                    self.log(f"  Fixed UNK_PARAMS: {func_name}({clean_params}) in {Path(hpath).name}")
                    fixed = True
                    break

        return fixed

    def _fix_redeclared_prototype(self, build_output: str, repo_dir: Path) -> bool:
        """Fix prototype redeclaration errors by updating header declarations.

        When the decompiled function has a different signature than the existing
        header declaration, mwcc reports a redeclaration error. This method parses
        those errors and updates the header to match the correct signature.

        Error format:
            identifier 'func_name(params)' redeclared
            was declared as: 'ret_type (old_params)'
            now declared as: 'ret_type (new_params)'

        Returns True if any fixes were made (caller should retry build).
        """
        # Normalize mwcc output - strip "#   " comment prefixes from lines
        build_output = re.sub(r'^#\s*', '', build_output, flags=re.MULTILINE)

        # Pattern to match redeclaration errors
        # Return type can be multi-word (e.g., "struct HSD_GObj *")
        pattern = (
            r"identifier '(\w+)\([^)]*\)' redeclared\s*"
            r"was declared as: '([^(]+) \(([^)]*)\)'\s*"
            r"now declared as: '([^(]+) \(([^)]*)\)'"
        )
        matches = re.findall(pattern, build_output, re.MULTILINE)

        if not matches:
            return False

        fixed = False
        seen = set()
        for func_name, old_ret, old_params, new_ret, new_params in matches:
            if func_name in seen:
                continue
            seen.add(func_name)

            # Clean up params - convert "struct X *" to "X*" for typedef form
            clean_params = re.sub(r'struct (\w+) \*', r'\1*', new_params)
            clean_params = re.sub(r'\s*,\s*', ', ', clean_params.strip())
            # Also clean up "void *" to "void*" for consistency
            clean_params = clean_params.replace('void *', 'void*')
            # Clean up return type - convert "struct X *" to "X*"
            clean_ret = re.sub(r'struct (\w+) \*', r'\1*', new_ret.strip())
            clean_ret = clean_ret.replace('void *', 'void*')

            # Search for ANY declaration of this function in headers
            for hpath in glob.glob(str(repo_dir / "src/**/*.h"), recursive=True):
                try:
                    htext = Path(hpath).read_text()
                except OSError:
                    continue

                # Match any prototype for this function:
                # /* ADDR */ ret_type func_name(...);
                # ret_type func_name(...);
                # Don't try to match exact params - they differ between header and compiler
                proto_pat = (
                    rf'(/\*[^*]*\*/\s*)?'          # optional addr comment
                    rf'(\w+[\s\*]*)\s+'             # return type (any)
                    rf'{re.escape(func_name)}'      # function name
                    rf'\s*\([^)]*\)\s*;'            # (any params);
                )
                m = re.search(proto_pat, htext)
                if m:
                    old_proto = m.group(0)
                    addr_comment = m.group(1) or ''
                    new_proto = f"{addr_comment}{clean_ret} {func_name}({clean_params});"
                    new_htext = htext.replace(old_proto, new_proto)
                    Path(hpath).write_text(new_htext)
                    self.log(f"  Fixed prototype: {func_name}({clean_params}) in {Path(hpath).name}")
                    fixed = True
                    break

        return fixed

    def _fix_unk_ret_from_illegal_operand(self, build_output: str, repo_dir: Path) -> bool:
        """Fix UNK_RET return type when function is used in comparison.

        When code compares a function's return value (e.g., `func() == 0`), but the
        function is declared with UNK_RET (which expands to `void`), mwcc reports
        "illegal operand". This method finds such functions and changes their return
        type from UNK_RET to s32.

        Error format example:
            In: src\melee\gm\gm_18A5.c
            732: } while (fn_801642A0() == 0);
            Error:                            ^
            illegal operand

        Returns True if any fixes were made (caller should retry build).
        """
        # Normalize mwcc output - strip "#   " comment prefixes from lines
        build_output = re.sub(r'^#\s*', '', build_output, flags=re.MULTILINE)

        # Check for "illegal operand" error
        if "illegal operand" not in build_output:
            return False

        # Look for the source line that has the error
        # Pattern: line number + source code containing a function call comparison
        # e.g., "732: } while (fn_801642A0() == 0);"
        line_pattern = r'(\d+):\s*[^;]*\b(\w+)\(\)\s*[=!]=\s*\d+'
        matches = re.findall(line_pattern, build_output)

        if not matches:
            return False

        fixed = False
        seen = set()
        for line_num, func_name in matches:
            if func_name in seen:
                continue
            seen.add(func_name)

            # Find header with UNK_RET prototype for this function
            for hpath in glob.glob(str(repo_dir / "src/**/*.h"), recursive=True):
                try:
                    htext = Path(hpath).read_text()
                except OSError:
                    continue

                # Match: /* ADDR */ UNK_RET func_name(...);
                proto_pat = (
                    rf'(/\*.*?\*/\s+)'             # addr comment
                    rf'UNK_RET\s+'                  # UNK_RET return type
                    rf'{re.escape(func_name)}'     # function name
                    rf'(\s*\([^)]*\)\s*;)'         # (params);
                )
                m = re.search(proto_pat, htext)
                if m:
                    old_proto = m.group(0)
                    addr_comment = m.group(1)
                    params_semicolon = m.group(2)
                    new_proto = f"{addr_comment}s32 {func_name}{params_semicolon}"
                    new_htext = htext.replace(old_proto, new_proto)
                    Path(hpath).write_text(new_htext)
                    self.log(f"  Fixed UNK_RET: {func_name} -> s32 in {Path(hpath).name}")
                    fixed = True
                    break

        return fixed

    def integrate_and_commit(self, func_name: str, sandbox_pct: float = 100.0) -> bool:
        """Integrate function into source tree, verify, and commit.

        Args:
            func_name: Function to integrate.
            sandbox_pct: Best match percentage from sandbox (80-100).
        """
        env = os.environ.copy()
        if self.melee_repo:
            env["MELEE_REPO"] = self.melee_repo

        # Run integrate command
        try:
            result = subprocess.run(
                [sys.executable, str(TOOLS_PY), "integrate", func_name],
                capture_output=True, text=True, timeout=60, env=env
            )
        except subprocess.TimeoutExpired:
            self.log(f"  integrate timed out")
            return False

        if result.returncode != 0:
            self.log(f"  integrate failed: {result.stderr.strip()}")
            return False

        self.log(f"  Integrated into source tree")

        # Run ninja to rebuild (with UNK_PARAMS fix-and-retry)
        repo_dir = Path(self.melee_repo) if self.melee_repo else None
        if not repo_dir:
            self.log("  No MELEE_REPO set, skipping ninja + verify")
            return True

        max_build_retries = 3
        for build_attempt in range(max_build_retries):
            try:
                result = subprocess.run(
                    ["ninja"],
                    cwd=repo_dir,
                    capture_output=True, text=True,
                    timeout=300
                )
            except subprocess.TimeoutExpired:
                self.log(f"  ninja timed out")
                subprocess.run(["git", "checkout", "--", "."], cwd=repo_dir,
                               capture_output=True, timeout=30)
                return False

            if result.returncode == 0:
                break  # Build succeeded

            # Build failed - check if prototype mismatch is fixable
            # Combine stdout and stderr since errors may be split between them
            errors = (result.stdout or "") + (result.stderr or "")
            if build_attempt < max_build_retries - 1:
                if self._fix_unk_params_from_build_errors(errors, repo_dir):
                    self.log(f"  Retrying build after UNK_PARAMS fix...")
                    continue  # Retry build
                if self._fix_redeclared_prototype(errors, repo_dir):
                    self.log(f"  Retrying build after prototype fix...")
                    continue  # Retry build
                if self._fix_unk_ret_from_illegal_operand(errors, repo_dir):
                    self.log(f"  Retrying build after UNK_RET fix...")
                    continue  # Retry build

            # Final failure or unfixable error
            self.log(f"  BUILD FAILED. Check compiler errors.")
            for line in errors.splitlines():
                if 'Error:' in line or 'redeclared' in line:
                    self.log(f"  {line.strip()}")
            self.log(f"  {errors[-500:]}" if len(errors) > 500 else f"  {errors}")
            # Revert uncommitted changes to avoid polluting the worktree
            subprocess.run(["git", "checkout", "--", "."], cwd=repo_dir,
                           capture_output=True, timeout=30)
            return False

        # Run verify
        verify_pct = None
        try:
            result = subprocess.run(
                [sys.executable, str(TOOLS_PY), "verify", func_name],
                capture_output=True, text=True, timeout=60, env=env
            )
            # Extract percentage from verify output
            import re
            pct_match = re.search(r'(\d+(?:\.\d+)?)%', result.stdout)
            if pct_match:
                verify_pct = float(pct_match.group(1))
        except subprocess.TimeoutExpired:
            self.log(f"  verify timed out")
            return False

        if verify_pct is not None and verify_pct >= 100.0:
            self.log(f"  VERIFIED 100% match!")
        elif verify_pct is not None and verify_pct >= 50.0:
            self.log(f"  Verify: {verify_pct:.2f}% (sandbox was {sandbox_pct:.2f}%)")
        else:
            verify_str = f"{verify_pct:.2f}%" if verify_pct is not None else "unknown"
            self.log(f"  VERIFY FAILED: {verify_str} (sandbox was {sandbox_pct:.2f}%)")
            # Revert on verify failure
            subprocess.run(["git", "checkout", "--", "."], cwd=repo_dir,
                           capture_output=True, timeout=30)
            return False

        # Run quality check
        try:
            result = subprocess.run(
                [sys.executable, str(MELEE_AI / "quality_check.py"), func_name],
                capture_output=True, text=True, timeout=30, env=env
            )
            if result.returncode != 0:
                self.log(f"  Quality warnings: {result.stdout.strip()}")
                # Don't fail on quality warnings, just log them
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # Quality check is optional

        # Commit if enabled
        if not self.no_commit:
            effective_pct = verify_pct if verify_pct is not None else sandbox_pct
            if effective_pct >= 100.0:
                msg = f"Match {func_name} (vacuum)"
            else:
                pct_str = f"{effective_pct:.0f}" if effective_pct == int(effective_pct) else f"{effective_pct:.1f}"
                msg = f"Decompile {func_name} ({pct_str}%, vacuum)"
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=repo_dir,
                    capture_output=True, timeout=30
                )
                subprocess.run(
                    ["git", "commit", "-m", msg],
                    cwd=repo_dir,
                    capture_output=True, text=True, timeout=30
                )
                self.log(f"  Committed: {msg}")
            except subprocess.TimeoutExpired:
                self.log(f"  git commit timed out")

        return True

    def add_to_skip_list(self, func_name: str, best_pct: float = 0.0,
                          attempts: int = 0):
        """Add a function to the difficult_functions skip list."""
        date = datetime.now().strftime("%Y-%m-%d")
        entry = f"{func_name} {date}"
        if attempts > 0:
            entry += f" attempts:{attempts}"
        if best_pct > 0:
            entry += f" best:{best_pct:.0f}%"
        entry += "\n"

        with open(DIFFICULT_FUNCTIONS, "a") as f:
            f.write(entry)

    def get_attempt_count(self, sandbox_dir: Path) -> int:
        """Count how many attempts were made in a sandbox."""
        match_log = sandbox_dir / "match_log.txt"
        if not match_log.exists():
            return 0
        return len([l for l in match_log.read_text().strip().split('\n') if l.strip()])

    def get_best_pct(self, sandbox_dir: Path) -> float:
        """Get the best match percentage from a sandbox."""
        match_log = sandbox_dir / "match_log.txt"
        if not match_log.exists():
            return 0.0

        best = 0.0
        for line in match_log.read_text().strip().split('\n'):
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    pct = float(parts[1].rstrip('%'))
                    best = max(best, pct)
                except ValueError:
                    continue
        return best

    def cleanup_sandbox(self, func_name: str):
        """Remove a sandbox directory."""
        sandbox_dir = SANDBOXES_DIR / func_name
        if sandbox_dir.exists():
            import shutil
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    def run(self):
        """Main vacuum loop."""
        # Acquire lock for this worktree
        lock_info = f"unit={self.unit_filter or 'all'} started={datetime.now().isoformat()}"
        if not acquire_vacuum_lock(self.melee_repo, lock_info):
            return  # Another vacuum is running on this worktree

        try:
            self._run_loop()
        finally:
            release_vacuum_lock(self.melee_repo)

    def _run_loop(self):
        """Internal run loop (called after lock acquired)."""
        self.log("=" * 60)
        self.log("Vacuum starting")
        self.log(f"  Branch: {self.branch_name}")
        self.log(f"  Max iterations: {self.max_iterations or 'unlimited'}")
        self.log(f"  Timeout per function: {self.timeout}s")
        self.log(f"  Dry run: {self.dry_run}")
        self.log(f"  Min similarity: {self.min_similarity or '(default)'}")
        self.log(f"  Max instructions: {self.max_instructions or '(default)'}")
        self.log(f"  Unit filter: {self.unit_filter or '(all)'}")
        self.log(f"  MELEE_REPO: {self.melee_repo or '(default)'}")
        self.log("=" * 60)

        iteration = 0
        consecutive_failures = 0

        pick_failures = 0
        max_pick_failures = 20  # Avoid infinite loop if all candidates are stale

        while self.running:
            if self.max_iterations and iteration >= self.max_iterations:
                self.log(f"Reached max iterations ({self.max_iterations})")
                break

            # Backoff on consecutive Claude failures (not pick failures)
            if consecutive_failures >= 3:
                wait = min(30 * consecutive_failures, 300)
                self.log(f"Backing off for {wait}s after {consecutive_failures} consecutive failures...")
                time.sleep(wait)

            # Pick next function
            func = self.pick_function()
            if not func:
                self.log("No more candidates available. Done.")
                break

            self.current_func = func
            self.attempted.add(func)

            # Try to create sandbox -- if it fails, skip and pick again
            # without counting toward --max iterations
            if not self.create_sandbox(func):
                self.log(f"  Sandbox failed for {func}, skipping (not counted)")
                self.add_to_skip_list(func)
                pick_failures += 1
                if pick_failures >= max_pick_failures:
                    self.log(f"Too many pick failures ({pick_failures}). Stopping.")
                    break
                self.current_func = None
                continue

            pick_failures = 0  # Reset on successful sandbox
            iteration += 1
            self.log(f"\n[{iteration}] Attempting: {func}")

            best_pct = self.attempt_function_in_sandbox(func)
            sandbox_dir = SANDBOXES_DIR / func

            # Run quality review pass on 100% matches
            if best_pct >= 100.0:
                self.quality_review_in_sandbox(func)
                # Re-check best match after quality review
                _, best_pct = self.get_best_matched_file(sandbox_dir)

            if best_pct >= 50.0:
                integrated = self.integrate_and_commit(func, sandbox_pct=best_pct)
                if integrated:
                    self.matched.append(func)
                    consecutive_failures = 0
                    if best_pct >= 100.0:
                        self.log(f"  SUCCESS: {func} matched and integrated")
                    else:
                        self.log(f"  SUCCESS: {func} decompiled ({best_pct:.1f}%) and integrated")
                else:
                    self.failed.append(func)
                    consecutive_failures += 1
                    self.log(f"  FAILED: {func} ({best_pct:.1f}% in sandbox but integration failed)")
                    self.add_to_skip_list(func,
                                          best_pct=best_pct,
                                          attempts=self.get_attempt_count(sandbox_dir))
            else:
                self.failed.append(func)
                consecutive_failures += 1
                attempts = self.get_attempt_count(sandbox_dir)
                self.add_to_skip_list(func, best_pct=best_pct, attempts=attempts)
                self.log(f"  FAILED: {func} (best: {best_pct:.0f}%, attempts: {attempts})")

            # Always keep sandboxes for manual review
            if best_pct < 50.0:
                self.log(f"  Keeping sandbox ({best_pct:.0f}% best) for manual review")

            self.current_func = None

        self.print_summary()

    def print_summary(self):
        """Print final summary of vacuum run."""
        self.log("")
        self.log("=" * 60)
        self.log("VACUUM SUMMARY")
        self.log("=" * 60)
        self.log(f"  Matched:  {len(self.matched)}")
        self.log(f"  Failed:   {len(self.failed)}")
        self.log(f"  Total:    {len(self.matched) + len(self.failed)}")

        if self.matched:
            self.log("")
            self.log("Matched functions:")
            for func in self.matched:
                self.log(f"  + {func}")

        if self.failed:
            self.log("")
            self.log("Failed functions:")
            for func in self.failed:
                self.log(f"  - {func}")

        # List kept sandboxes (promising partial matches)
        kept = []
        for func in self.failed:
            sandbox_dir = SANDBOXES_DIR / func
            if sandbox_dir.exists():
                best = self.get_best_pct(sandbox_dir)
                kept.append((func, best))
        if kept:
            self.log("")
            self.log("Kept sandboxes (review manually or run permuter):")
            for func, pct in kept:
                self.log(f"  ~ {func} ({pct:.0f}%) -> melee-ai/sandboxes/{func}/")

        self.log("=" * 60)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Autonomous decompilation vacuum - processes easy functions in a loop."
    )
    parser.add_argument("--max", type=int, default=None,
                        help="Maximum number of functions to attempt")
    parser.add_argument("--branch", type=str, default=None,
                        help="Git branch name")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout per function attempt in seconds (default: 600)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pick functions and create sandboxes but don't invoke Claude")
    parser.add_argument("--no-commit", action="store_true",
                        help="Skip git commits after successful matches")
    parser.add_argument("--melee-repo", type=str, default=None,
                        help="Path to melee repo (or set MELEE_REPO env)")
    parser.add_argument("--min-similarity", type=float, default=None,
                        help="Minimum similarity score for candidates (default: 0.90)")
    parser.add_argument("--max-instructions", type=int, default=None,
                        help="Maximum instruction count for candidates (default: 50)")
    parser.add_argument("--unit", type=str, default=None,
                        help="Filter candidates to units matching this substring (e.g., 'ftKirby', 'itfreeze')")
    parser.add_argument("--relaxed", action="store_true",
                        help="Use relaxed parameters: min-similarity=0.5, max-instructions=99999, timeout=3600")

    args = parser.parse_args()

    # Apply relaxed preset if specified
    if args.relaxed:
        if args.min_similarity is None:
            args.min_similarity = 0.5
        if args.max_instructions is None:
            args.max_instructions = 99999
        if args.timeout == 600:  # Only override if still default
            args.timeout = 3600

    vacuum = Vacuum(
        max_iterations=args.max,
        branch_name=args.branch,
        timeout=args.timeout,
        dry_run=args.dry_run,
        no_commit=args.no_commit,
        melee_repo=args.melee_repo,
        min_similarity=args.min_similarity,
        max_instructions=args.max_instructions,
        unit_filter=args.unit,
    )

    vacuum.run()


if __name__ == "__main__":
    main()
