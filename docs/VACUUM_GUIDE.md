# Vacuum System Guide

The vacuum automatically decompiles Melee functions: picks candidates, creates sandboxes, invokes Claude CLI, and integrates matches.

## Prerequisites

```bash
pip install numpy  # Required for embeddings
```

## Quick Start

```bash
cd ~/melee-decomp-agent

# One command sets up everything (creates worktree, copies orig, builds)
python3 melee-ai/tools.py setup-worktree my-session

# Run the vacuum
python3 melee-ai/vacuum.py --max 15 --melee-repo .worktrees/my-session
```

## Parameter Presets

**Standard** (default): Conservative, high-confidence matches only.
```bash
python3 melee-ai/vacuum.py --max 15 --melee-repo /path/to/worktree
# min-similarity=0.90, max-instructions=50, timeout=600
```

**Relaxed**: Attempts more functions including complex/low-similarity ones.
```bash
python3 melee-ai/vacuum.py --max 100 --relaxed --melee-repo /path/to/worktree
# min-similarity=0.5, max-instructions=99999, timeout=3600
```

## All Options

| Flag | Default | Description |
|------|---------|-------------|
| `--max N` | unlimited | Max functions to attempt |
| `--timeout SECS` | 600 | Timeout per function |
| `--melee-repo DIR` | env var | Path to melee worktree |
| `--relaxed` | off | Use relaxed parameters |
| `--unit UNIT` | all | Filter to specific compilation unit |
| `--min-similarity F` | 0.90 | Minimum similarity score |
| `--max-instructions N` | 50 | Max instruction count |
| `--dry-run` | off | Sandbox only, don't invoke Claude |
| `--no-commit` | off | Skip git commits |

## Background Operation

```bash
nohup python3 melee-ai/vacuum.py --max 30 --relaxed --melee-repo /path > /dev/null 2>&1 &

# Monitor
tail -f melee-ai/vacuum_logs/vacuum-*.log
grep -E '(SUCCESS|FAILED|SUMMARY)' melee-ai/vacuum_logs/vacuum-*.log
```

## Manual Tools

```bash
# See candidates
python3 melee-ai/tools.py vacuum-pick 30

# Create sandbox manually
python3 melee-ai/tools.py sandbox <func_name>

# Integrate a sandbox result (50%+ match required)
export MELEE_REPO=/melee-decompile
python3 melee-ai/tools.py integrate <func_name>
ninja && python3 melee-ai/tools.py verify <func_name>

# Skip list management
python3 melee-ai/tools.py skip <func> "reason"
python3 melee-ai/tools.py skip --clean
```

## Common Failures

| Log message | Action |
|-------------|--------|
| `Claude CLI timed out` | Use `--relaxed` or try manually |
| `BUILD FAILED` + `does not match` | Header prototype mismatch (usually auto-fixed) |
| `VERIFY FAILED` | Header inlines differ; try manual permuter |
| `best: 0%` | Function too complex for automation |

## Failed Integration Fixes

When sandbox matches but integration fails:

1. **Prototype mismatch**: Find and fix the header declaration
   ```bash
   grep -r "func_name" src/melee/*/*.h
   ```

2. **Undefined externs**: Leave as stub until data section is defined

3. **Stack size mismatch**: Add `PAD_STACK(N)` after integration

## Tips

- **COMMIT FIRST** before running vacuum - it modifies the worktree
- Integrate ALL 50%+ matches, not just 100%
- Clean sandboxes between runs: `rm -rf melee-ai/sandboxes/*`
