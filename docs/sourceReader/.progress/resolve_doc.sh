#!/usr/bin/env bash
# Resolve all file.go:NNN refs in a doc using the shared overrides.
# Usage: bash resolve_doc.sh <doc.md>
cd "$(git rev-parse --show-toplevel)"
OVERRIDES=$(grep -vE '^#|^$|is dual' docs/sourceReader/.progress/overrides.txt | tr '\n' ' ')
python3 docs/sourceReader/.progress/go_resolve.py "$1" $OVERRIDES