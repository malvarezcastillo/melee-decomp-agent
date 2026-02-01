# Setup Guide

This guide covers setting up the melee-decomp-agent environment from scratch.

## Prerequisites

**Linux (Ubuntu 24.04):**
- Python 3.10+
- Git with LFS support
- Claude Code CLI

**macOS (Apple Silicon):**
- Python 3.10+ (via Homebrew)
- Git with LFS support
- Homebrew
- Rosetta 2 (for wibo)
- Claude Code CLI

---

## Linux Setup

### 1. Clone the Repository

```bash
# Install Git LFS first
sudo apt-get install git-lfs
git lfs install

# Clone with LFS
git clone https://github.com/malvarezcastillo/melee-decomp-agent.git
cd melee-decomp-agent
```

### 2. Clone the Melee Decomp Project

The main decomp project is separate (not a submodule):

```bash
git clone https://github.com/doldecomp/melee.git
cd melee
```

Follow the [melee decomp setup instructions](https://github.com/doldecomp/melee#building) to configure the build environment. Key steps:

```bash
# Install dependencies
sudo apt-get install ninja-build

# Download tools (mwcc compiler, wibo, etc.)
python3 configure.py
ninja

# Verify build works
ninja diff
```

---

## macOS Setup (Apple Silicon)

### 1. Install Prerequisites

```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install dependencies
brew install git-lfs ninja python@3.12

# Install Rosetta 2 (required for wibo)
softwareupdate --install-rosetta --agree-to-license

# Set up Git LFS
git lfs install
```

### 2. Clone the Repositories

```bash
# Clone this repo
git clone https://github.com/malvarezcastillo/melee-decomp-agent.git
cd melee-decomp-agent

# Clone the melee decomp project inside it
git clone https://github.com/doldecomp/melee.git
```

### 3. Set Up Python Virtual Environment

macOS requires a venv due to PEP 668 (system Python is locked):

```bash
cd melee
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip certifi
```

### 4. Copy Original Game Files

You need the original SSBM v1.02 game files. Copy them to:

```
melee/orig/GALE01/sys/
├── apploader.img
├── bi2.bin
├── boot.bin
├── fst.bin
└── main.dol
```

### 5. Download Tools and Build

The decomp.dev server has SSL certificate issues. Download compilers manually:

```bash
# Download compilers (check for latest tag at files.decomp.dev)
curl -kL -o /tmp/compilers.zip "https://files.decomp.dev/compilers_20250520.zip"
mkdir -p build/compilers build/tools build/binutils
unzip -q /tmp/compilers.zip -d build/
rm /tmp/compilers.zip

# Download wibo (lightweight Windows binary runner for macOS)
curl -kL -o build/tools/wibo "https://github.com/decompals/wibo/releases/download/1.0.1/wibo-macos"
chmod +x build/tools/wibo

# Download macOS binutils
curl -kL -o /tmp/binutils.zip "https://github.com/encounter/gc-wii-binutils/releases/download/2.42-1/macos-universal.zip"
unzip -q /tmp/binutils.zip -d build/binutils
chmod +x build/binutils/*
rm /tmp/binutils.zip
```

### 6. Configure with wibo (Critical for Performance!)

**Important:** Configure with `--wrapper` to use wibo instead of Wine. This enables parallel builds.

```bash
source .venv/bin/activate
python3 configure.py --wrapper build/tools/wibo
ninja -j16  # Use your core count
```

**Performance comparison:**
| Setup | Build Time |
|-------|------------|
| Wine `-j1` | ~45 minutes |
| wibo `-j16` | ~58 seconds |

### 7. Verify Setup

```bash
source .venv/bin/activate
ninja diff  # Should show no ERROR lines

# Test tools
python3 ../melee-ai/tools.py verify ftMt_SpecialHi_CreateGFX
python3 ../melee-ai/tools.py scratch ftMt_SpecialHi_CreateGFX
```

### macOS Troubleshooting

**"wine: command not found" during build:**
You configured without `--wrapper`. Reconfigure:
```bash
python3 configure.py --wrapper build/tools/wibo
```

**SSL certificate errors when downloading:**
The decomp.dev server has a self-signed certificate. Use `curl -k` to bypass:
```bash
curl -kL -o file.zip "https://files.decomp.dev/..."
```

**Build fails with parallel jobs:**
If using Wine instead of wibo, you must use `-j1`. Switch to wibo (see step 6).

**"No such file or directory" for binutils:**
Download macOS binutils (see step 5). The Linux binaries won't work on macOS.

---

## Common Setup (Both Platforms)

### Set Up the Permuter (Optional)

For register allocation issues, you'll need the decomp-permuter:

```bash
cd ~
git clone https://github.com/simonlindholm/decomp-permuter.git
cd decomp-permuter

# Linux
pip3 install --user pycparser

# macOS (use venv)
pip3 install pycparser
```

### Set Up Voyage AI API Key (For Similarity Search)

The `recommend` and `similar` commands use Voyage AI embeddings. Get an API key from https://www.voyageai.com/ and set it:

```bash
export VOYAGE_API_KEY="your-api-key-here"

# Add to your shell profile for persistence
# Linux:
echo 'export VOYAGE_API_KEY="your-api-key-here"' >> ~/.bashrc
# macOS:
echo 'export VOYAGE_API_KEY="your-api-key-here"' >> ~/.zshrc
```

### Set Up Claude Code Skill (Required for /melee-decompile)

To use the `/melee-decompile` slash command in Claude Code, create a symlink from your Claude commands directory to the skill file:

```bash
# Create the commands directory
mkdir -p ~/.claude/commands

# Create symlink (adjust the source path to where you cloned melee-decomp-agent)
ln -s /path/to/melee-decomp-agent/.claude-commands/melee-decompile.md ~/.claude/commands/melee-decompile.md

# Example if cloned to ~/melee-decomp-agent:
# ln -s ~/melee-decomp-agent/.claude-commands/melee-decompile.md ~/.claude/commands/melee-decompile.md

# Example if cloned to ~/dev/melee-decomp-agent:
# ln -s ~/dev/melee-decomp-agent/.claude-commands/melee-decompile.md ~/.claude/commands/melee-decompile.md
```

Verify the symlink works:
```bash
ls -la ~/.claude/commands/melee-decompile.md
```

### Verify Setup

```bash
# Check tools work
cd ~/melee-decomp-agent
python3 melee-ai/tools.py recommend

# Check build works
cd melee
ninja  # Add -j16 on macOS with wibo
python3 ~/melee-decomp-agent/melee-ai/tools.py verify <any-function>
```

## Directory Structure

After setup, your directory should look like:

```
~/melee-decomp-agent/            # This repo
├── melee/                       # doldecomp/melee (cloned inside)
│   ├── .venv/                   # Python venv (macOS only)
│   ├── build/
│   │   ├── compilers/           # mwcc compiler
│   │   ├── tools/
│   │   │   ├── wibo             # Windows binary runner (macOS)
│   │   │   ├── dtk              # decomp-toolkit
│   │   │   └── sjiswrap.exe     # Shift-JIS wrapper
│   │   └── binutils/            # PowerPC binutils
│   ├── orig/GALE01/sys/         # Original game files
│   └── src/                     # Decompiled source code
├── melee-ai/                    # Tools and embeddings cache
├── context.txt                  # Community context file
├── .claude-commands/            # Claude Code skills
└── CLAUDE.md                    # Claude Code instructions

~/decomp-permuter/               # Optional, for register allocation
```

## Troubleshooting

### Git LFS issues

If you see "This repository is configured for Git LFS but 'git-lfs' was not found":

```bash
# Linux
sudo apt-get install git-lfs

# macOS
brew install git-lfs

# Both platforms
git lfs install
git lfs pull
```

### Missing mwcc compiler

```bash
cd melee
python3 configure.py
ninja tools
```

### Build errors

```bash
cd melee
python3 configure.py --clean
python3 configure.py  # Add --wrapper build/tools/wibo on macOS
ninja
```

### Embeddings cache not loading

The embeddings cache (`melee-ai/.embeddings_cache.json`) is stored in Git LFS. If it's not downloading:

```bash
git lfs pull
```

To refresh embeddings after many new functions are matched:

```bash
python3 melee-ai/tools.py index --refresh
```

### macOS: Python SSL certificate errors

If Python fails to download files with SSL errors, the server likely has certificate issues. Use curl instead:

```bash
curl -kL -o output.zip "https://url-here"
```

### macOS: Slow builds (must use -j1)

You're using Wine instead of wibo. Reconfigure:

```bash
python3 configure.py --wrapper build/tools/wibo
ninja -j16
```
