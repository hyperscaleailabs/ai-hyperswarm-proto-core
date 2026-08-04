#!/usr/bin/env python3
"""Verify governance artifacts for block 41337"""

import json
import sys
from pathlib import Path

def verify_iterations_jsonl(path):
    """Verify iterations.jsonl has valid JSON lines"""
    print(f"Checking {path}...")
    lines = path.read_text().strip().split('\n')

    block_41337_entries = 0
    for i, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
            if entry.get('block') == 41337:
                block_41337_entries += 1
            print(f"  Line {i}: ✓ Valid JSON - iteration {entry.get('iteration')}, block {entry.get('block')}")
        except json.JSONDecodeError as e:
            print(f"  Line {i}: ✗ Invalid JSON - {e}")
            return False

    if block_41337_entries != 3:
        print(f"  ✗ Expected 3 block 41337 entries, found {block_41337_entries}")
        return False

    print(f"  ✓ Found {block_41337_entries} block 41337 entries")
    return True

def verify_markdown_file(path):
    """Verify markdown file exists and has frontmatter"""
    print(f"Checking {path}...")

    if not path.exists():
        print(f"  ✗ File does not exist")
        return False

    content = path.read_text()

    # Check for frontmatter
    if not content.startswith('---'):
        print(f"  ✗ Missing frontmatter")
        return False

    if content.count('---') < 2:
        print(f"  ✗ Incomplete frontmatter")
        return False

    # Check for content
    parts = content.split('---', 2)
    if len(parts[2].strip()) == 0:
        print(f"  ✗ No content after frontmatter")
        return False

    print(f"  ✓ Valid markdown with {len(content.split())} words")
    return True

def verify_direction_md(path):
    """Verify DIRECTION.md has all required references"""
    print(f"Checking {path}...")

    content = path.read_text()

    required_refs = ['#61', '#71', '#62', '#63', '#64', '#69', '#73']
    missing = []

    for ref in required_refs:
        if ref not in content:
            missing.append(ref)

    if missing:
        print(f"  ✗ Missing references: {missing}")
        return False

    required_strings = [
        'Updated: 2026-08-03',
        'review: block 41335',
        'review: block 41337',
        '_(BLOCKED)_',
    ]

    for s in required_strings:
        if s not in content:
            print(f"  ✗ Missing: '{s}'")
            return False

    print(f"  ✓ Contains all required references and updates")
    return True

def verify_mocs(base_path):
    """Verify MOC files have updated timestamps"""
    print(f"Checking MOCs...")

    moc_files = [
        base_path / "knowledge/MOCs/Knowledge Base MOC.md",
        base_path / "knowledge/MOCs/Lessons MOC.md",
        base_path / "knowledge/MOCs/Whitepapers MOC.md",
    ]

    for path in moc_files:
        content = path.read_text()
        if 'updated: 2026-08-03' not in content:
            print(f"  ✗ {path.name}: Missing updated: 2026-08-03")
            return False
        print(f"  ✓ {path.name}: Updated timestamp correct")

    return True

def main():
    root = Path.cwd()

    all_ok = True

    # Verify iterations.jsonl
    all_ok &= verify_iterations_jsonl(root / "knowledge/ledger/iterations.jsonl")
    print()

    # Verify whitepaper
    all_ok &= verify_markdown_file(root / "knowledge/whitepapers/2026-08-03-synthesis-after-15-lessons.md")
    print()

    # Verify persona articles
    articles = [
        root / "knowledge/articles/2026-08-03-synthesis-after-15-lessons-architect.md",
        root / "knowledge/articles/2026-08-03-synthesis-after-15-lessons-cto.md",
        root / "knowledge/articles/2026-08-03-synthesis-after-15-lessons-devops.md",
    ]

    for article in articles:
        all_ok &= verify_markdown_file(article)
    print()

    # Verify DIRECTION.md
    all_ok &= verify_direction_md(root / "governance/DIRECTION.md")
    print()

    # Verify MOCs
    all_ok &= verify_mocs(root)
    print()

    if all_ok:
        print("✓ All governance artifacts for block 41337 are valid!")
        return 0
    else:
        print("✗ Some governance artifacts have issues")
        return 1

if __name__ == "__main__":
    sys.exit(main())
