#!/usr/bin/env python3
"""Batch permuter runner. Runs the permuter on each function for a fixed duration."""

import argparse
import fcntl
import os
import re
import signal
import subprocess
import sys
import time


def set_nonblocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def parse_scores(text):
    """Extract best score from permuter output text."""
    scores = []
    # Match patterns like "base score = 25", "score = 20", "score: 48"
    for m in re.finditer(r'score\s*[=:]\s*(\d+)', text, re.IGNORECASE):
        scores.append(int(m.group(1)))
    return scores


def run_permuter(func, duration_secs, jobs, permuter_dir):
    """Run the permuter for a single function, returning (func, best_score, improved, status)."""
    nonmatch_dir = os.path.join(permuter_dir, "nonmatchings", func)
    if not os.path.isdir(nonmatch_dir):
        return func, None, False, "directory not found"

    cmd = [
        sys.executable, os.path.join(permuter_dir, "permuter.py"),
        nonmatch_dir,
        f"-j{jobs}",
        "--better-only",
        "--stop-on-zero",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=permuter_dir,
            preexec_fn=os.setsid,
        )
        set_nonblocking(proc.stdout.fileno())

        all_output = b""
        deadline = time.monotonic() + duration_secs

        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
            try:
                chunk = proc.stdout.read(65536)
                if chunk:
                    all_output += chunk
            except (BlockingIOError, OSError):
                pass

        # Kill the process group
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()

        # Drain remaining output
        try:
            rest = proc.stdout.read()
            if rest:
                all_output += rest
        except (BlockingIOError, OSError):
            pass

        text = all_output.decode("utf-8", errors="replace")
        scores = parse_scores(text)

        if not scores:
            # Grab last meaningful line for error reporting
            lines = [l.strip() for l in text.replace('\r', '\n').split('\n') if l.strip()]
            last_line = lines[-1] if lines else "no output"
            return func, None, False, last_line

        base_score = scores[0]
        best_score = min(scores)
        improved = best_score < base_score

        status = "improved" if improved else "ok"
        return func, best_score, improved, status

    except Exception as e:
        return func, None, False, str(e)


def main():
    parser = argparse.ArgumentParser(description="Batch permuter runner")
    parser.add_argument("input", help="File with function names (one per line), or '-' for stdin")
    parser.add_argument("-t", "--time", type=int, default=120, help="Seconds per function (default: 120)")
    parser.add_argument("-j", "--jobs", type=int, default=4, help="Parallel threads for permuter (default: 4)")
    parser.add_argument("-d", "--permuter-dir", default=os.path.expanduser("~/decomp-permuter"),
                        help="Path to decomp-permuter (default: ~/decomp-permuter)")
    parser.add_argument("-o", "--output", help="Write results summary to file")
    parser.add_argument("--skip-missing", action="store_true", help="Skip functions without permuter dirs")
    args = parser.parse_args()

    if args.input == "-":
        functions = [l.strip() for l in sys.stdin if l.strip() and not l.startswith("#")]
    else:
        with open(args.input) as f:
            functions = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    if not functions:
        print("No functions to process.")
        return

    total = len(functions)
    results = []

    print(f"Batch permuter: {total} functions, {args.time}s each, {args.jobs} threads")
    print(f"Estimated total: {total * args.time // 60} minutes")
    print("-" * 70)

    for i, func in enumerate(functions, 1):
        nonmatch_dir = os.path.join(args.permuter_dir, "nonmatchings", func)
        if not os.path.isdir(nonmatch_dir):
            if args.skip_missing:
                print(f"[{i}/{total}] {func}: SKIPPED (no dir)")
                results.append((func, None, False, "skipped"))
                continue
            else:
                print(f"[{i}/{total}] {func}: MISSING dir {nonmatch_dir}")
                results.append((func, None, False, "missing"))
                continue

        print(f"[{i}/{total}] {func}: running for {args.time}s...", end=" ", flush=True)
        func_name, score, improved, status = run_permuter(func, args.time, args.jobs, args.permuter_dir)

        if score is not None:
            marker = " *IMPROVED*" if improved else ""
            print(f"score={score}{marker}")
        else:
            print(f"{status}")

        results.append((func_name, score, improved, status))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    zeros = [(f, s) for f, s, _, _ in results if s == 0]
    improved_list = [(f, s) for f, s, imp, _ in results if imp]
    failed = [(f, st) for f, s, _, st in results if s is None]

    if zeros:
        print(f"\nPERFECT MATCH ({len(zeros)}):")
        for f, _ in zeros:
            print(f"  {f}")

    if improved_list:
        print(f"\nIMPROVED ({len(improved_list)}):")
        for f, s in improved_list:
            print(f"  {f}: score={s}")

    scored = [(f, s) for f, s, _, _ in results if s is not None and s > 0]
    if scored:
        print(f"\nNOT YET MATCHED ({len(scored)}):")
        for f, s in sorted(scored, key=lambda x: x[1]):
            print(f"  {f}: score={s}")

    if failed:
        print(f"\nFAILED ({len(failed)}):")
        for f, st in failed:
            print(f"  {f}: {st}")

    if args.output:
        with open(args.output, "w") as out:
            out.write("function\tscore\timproved\tstatus\n")
            for f, s, imp, st in results:
                out.write(f"{f}\t{s}\t{imp}\t{st}\n")
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
