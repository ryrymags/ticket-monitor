#!/bin/bash
# Installs git hooks from scripts/hooks/ into .git/hooks/
# Run once after cloning: bash scripts/install_hooks.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HOOKS_SRC="$SCRIPT_DIR/hooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

if [ ! -d "$REPO_ROOT/.git" ]; then
    echo "Error: not a git repo root ($REPO_ROOT)"
    exit 1
fi

for hook in "$HOOKS_SRC"/*; do
    name="$(basename "$hook")"
    dest="$HOOKS_DST/$name"
    cp "$hook" "$dest"
    chmod +x "$dest"
    echo "Installed: .git/hooks/$name"
done

echo "Done. Git hooks are active."
