#!/usr/bin/env python3
"""
Batch m2c decompilation tool.
Runs m2c on all stubbed functions and scores the results to find easy wins.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Add tools.py to path
sys.path.insert(0, str(Path(__file__).parent))
from tools import (
    MELEE_REPO, OBJDIFF_JSON, CONTEXT_TXT,
    compile_local, score_object_files, has_local_compiler
)

RESULTS_FILE = Path(__file__).parent / "m2c_batch_results.json"
REPORT_JSON = MELEE_REPO / "build/GALE01/report.json"


def get_already_matched_functions() -> set[str]:
    """Get set of function names that are already 100% matched."""
    if not REPORT_JSON.exists():
        print(f"Warning: report.json not found, can't filter matched functions", file=sys.stderr)
        return set()

    with open(REPORT_JSON) as f:
        report = json.load(f)

    matched = set()
    for unit in report.get("units", []):
        for func in unit.get("functions", []):
            if func.get("fuzzy_match_percent", 0) == 100.0:
                matched.add(func.get("name", ""))

    return matched


def get_stubbed_functions() -> list[dict]:
    """Get all stubbed functions from source files."""
    if not OBJDIFF_JSON.exists():
        print(f"objdiff.json not found: {OBJDIFF_JSON}", file=sys.stderr)
        return []

    with open(OBJDIFF_JSON) as f:
        objdiff = json.load(f)

    stubbed = []
    for unit in objdiff.get("units", []):
        source_path = unit.get("metadata", {}).get("source_path", "")
        if not source_path:
            continue

        full_path = MELEE_REPO / source_path
        if not full_path.exists():
            continue

        target_path = MELEE_REPO / unit.get("target_path", "")
        if not target_path.exists():
            continue

        # Find the .s assembly file
        # target_path: build/GALE01/obj/melee/lb/lbshadow.o
        # asm_path: build/GALE01/asm/melee/lb/lbshadow.s
        rel_path = target_path.relative_to(MELEE_REPO / "build/GALE01/obj")
        asm_path = MELEE_REPO / "build/GALE01/asm" / rel_path.with_suffix(".s")

        if not asm_path.exists():
            continue

        # Read source to find stubbed functions: /// #func_name
        with open(full_path) as f:
            content = f.read()

        stubs = re.findall(r"///\s*#(\w+)", content)
        for func_name in stubs:
            stubbed.append({
                "name": func_name,
                "unit": unit.get("name", ""),
                "source_path": str(full_path),
                "target_path": str(target_path),
                "asm_path": str(asm_path),
            })

    return stubbed


def run_m2c(func_name: str, asm_path: str) -> str | None:
    """Run m2c on a function and return the decompiled C code."""
    try:
        result = subprocess.run(
            ["m2c", "-t", "ppc-mwcc-c", "-f", func_name, asm_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except Exception as e:
        return None


def score_m2c_output(func_name: str, m2c_code: str, target_path: str) -> dict:
    """Compile m2c output and score against target."""
    with tempfile.NamedTemporaryFile(suffix=".o", delete=False) as f:
        current_o = Path(f.name)

    try:
        success, error = compile_local(m2c_code, current_o, func_name)
        if not success:
            return {"error": f"compile: {error[:100]}"}

        target_o = Path(target_path)
        score, max_score, _ = score_object_files(target_o, current_o, func_name)

        if max_score > 0:
            match_pct = (1 - score / max_score) * 100
        else:
            match_pct = 100.0 if score == 0 else 0.0

        return {
            "score": score,
            "max_score": max_score,
            "match_pct": round(match_pct, 2),
        }
    except Exception as e:
        return {"error": str(e)[:100]}
    finally:
        current_o.unlink(missing_ok=True)


def process_function(func: dict) -> dict | None:
    """Process a single function: m2c -> compile -> score.

    Returns None if compilation fails (we don't care about those).
    """
    func_name = func["name"]
    asm_path = func["asm_path"]
    target_path = func["target_path"]

    # Run m2c
    m2c_code = run_m2c(func_name, asm_path)
    if not m2c_code:
        return None  # m2c failed, skip

    # Score the result
    result = score_m2c_output(func_name, m2c_code, target_path)

    # Only keep if compilation succeeded
    if "error" in result:
        return None  # compilation failed, skip

    result["name"] = func_name
    result["unit"] = func["unit"]
    result["m2c_code"] = m2c_code

    return result


def main():
    if not has_local_compiler():
        print("Local compiler not available", file=sys.stderr)
        sys.exit(1)

    # Check for existing results to resume
    existing_results = {}
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE) as f:
            data = json.load(f)
            existing_results = {r["name"]: r for r in data.get("results", [])}
        print(f"Loaded {len(existing_results)} existing results")

    # Get all stubbed functions
    print("Finding stubbed functions...")
    stubbed = get_stubbed_functions()
    print(f"Found {len(stubbed)} stubbed functions")

    # Filter out already matched functions (from report.json)
    print("Loading already-matched functions from report.json...")
    already_matched = get_already_matched_functions()
    print(f"Found {len(already_matched)} already-matched functions")

    stubbed = [f for f in stubbed if f["name"] not in already_matched]
    print(f"After filtering: {len(stubbed)} unmatched stubbed functions")

    # Filter out already processed
    to_process = [f for f in stubbed if f["name"] not in existing_results]
    print(f"Need to process {len(to_process)} functions")

    if not to_process:
        print("All functions already processed!")
        show_results(existing_results)
        return

    # Process functions
    results = list(existing_results.values())
    start_time = time.time()
    processed = 0
    compiled = 0
    skipped = 0

    # Use thread pool for parallel processing
    with ThreadPoolExecutor(max_workers=24) as executor:
        futures = {executor.submit(process_function, f): f for f in to_process}

        for future in as_completed(futures):
            result = future.result()
            processed += 1

            if result is None:
                skipped += 1  # m2c or compilation failed
            else:
                results.append(result)
                compiled += 1

            # Progress update every 100 functions
            if processed % 100 == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed
                remaining = len(to_process) - processed
                eta = remaining / rate if rate > 0 else 0
                print(f"Processed {processed}/{len(to_process)} "
                      f"({compiled} compiled, {skipped} skipped) - {rate:.1f}/s - ETA {eta:.0f}s",
                      flush=True)

                # Save intermediate results
                save_results(results)

    # Final save
    save_results(results)

    elapsed = time.time() - start_time
    print(f"\nCompleted {processed} functions in {elapsed:.1f}s")
    print(f"Compiled successfully: {compiled}")
    print(f"Skipped (m2c/compile failed): {skipped}")

    show_results({r["name"]: r for r in results})


def save_results(results: list):
    """Save results to JSON file."""
    with open(RESULTS_FILE, "w") as f:
        json.dump({"results": results}, f, indent=2)


def show_results(results: dict):
    """Show top matches from results."""
    # Filter to successful results with match %
    scored = [r for r in results.values() if "match_pct" in r]
    scored.sort(key=lambda x: x["match_pct"], reverse=True)

    print(f"\n=== Top 30 Easy Wins ===")
    print(f"{'Match %':<10} {'Function':<40} {'Unit'}")
    print("-" * 80)

    for r in scored[:30]:
        print(f"{r['match_pct']:>7.1f}%   {r['name']:<40} {r['unit']}")

    # Stats
    print(f"\n=== Statistics ===")
    if scored:
        avg = sum(r["match_pct"] for r in scored) / len(scored)
        above_90 = len([r for r in scored if r["match_pct"] >= 90])
        above_80 = len([r for r in scored if r["match_pct"] >= 80])
        above_50 = len([r for r in scored if r["match_pct"] >= 50])
        print(f"Average match: {avg:.1f}%")
        print(f">=90% match: {above_90}")
        print(f">=80% match: {above_80}")
        print(f">=50% match: {above_50}")


if __name__ == "__main__":
    main()
