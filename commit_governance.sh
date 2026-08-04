#!/bin/bash
# Commit governance artifacts for block 41337

set -e

echo "Staging governance artifact changes..."
git add governance/DIRECTION.md
git add "knowledge/MOCs/Knowledge Base MOC.md"
git add "knowledge/MOCs/Lessons MOC.md"
git add "knowledge/MOCs/Whitepapers MOC.md"
git add knowledge/ledger/iterations.jsonl
git add knowledge/articles/2026-08-03-*.md
git add knowledge/whitepapers/2026-08-03-*.md

echo "Creating commit..."
git commit -F .git_commit_message

echo "✓ Governance artifacts for block 41337 committed successfully"
