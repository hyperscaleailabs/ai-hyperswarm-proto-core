#!/usr/bin/env python3
"""Verify that the changes are syntactically correct and functional."""

import sys
sys.path.insert(0, 'src')

# Test 1: Import the module
try:
    from hsai.knowledge import format_reference, KnowledgeBase, Lesson
    print("✓ Module imports successfully")
except Exception as e:
    print(f"✗ Import failed: {e}")
    sys.exit(1)

# Test 2: Test format_reference function
test_cases = [
    ("langchain-ai/langchain", "[langchain-ai/langchain](https://github.com/langchain-ai/langchain)"),
    ("openai/swarm", "[openai/swarm](https://github.com/openai/swarm)"),
    ("  openai/swarm  ", "[openai/swarm](https://github.com/openai/swarm)"),
    ("invalid", "`invalid`"),
    ("", "``"),
]

for input_val, expected in test_cases:
    result = format_reference(input_val)
    if result == expected:
        print(f"✓ format_reference({input_val!r}) = {result!r}")
    else:
        print(f"✗ format_reference({input_val!r}) = {result!r}, expected {expected!r}")
        sys.exit(1)

# Test 3: Test lesson rendering with formatted references
try:
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = KnowledgeBase(tmpdir)
        lesson = Lesson(
            title="test lesson",
            outcome="pass",
            kind="improve",
            context="test context",
            what_happened="test what happened",
            lesson="test lesson text",
            references=("langchain-ai/langchain", "openai/swarm")
        )
        path = kb.write_lesson(lesson)
        content = path.read_text()

        # Check that links are in the output
        if "[langchain-ai/langchain](https://github.com/langchain-ai/langchain)" in content:
            print("✓ Lesson rendering includes formatted GitHub link")
        else:
            print("✗ Lesson rendering missing GitHub link")
            sys.exit(1)

        if "[openai/swarm](https://github.com/openai/swarm)" in content:
            print("✓ Multiple references are formatted correctly")
        else:
            print("✗ Multiple references not formatted correctly")
            sys.exit(1)

except Exception as e:
    print(f"✗ Lesson rendering test failed: {e}")
    sys.exit(1)

print("\n✓ All verification tests passed!")
