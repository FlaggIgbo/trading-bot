#!/usr/bin/env python3
"""Configures Git to use version-controlled hooks in .githooks/"""
import subprocess
import sys
from pathlib import Path

def main():
    repo_root = Path(__file__).resolve().parent.parent
    hook_dir = repo_root / ".githooks"
    
    if not hook_dir.exists():
        print(f"Error: {hook_dir} does not exist", file=sys.stderr)
        sys.exit(1)
        
    try:
        subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo_root, check=True)
        print("✅ Configured git core.hooksPath -> .githooks")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to configure git hooks: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
