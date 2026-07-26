#!/usr/bin/env python
"""Quick verification that the practices module is importable and works."""

try:
    from src.hsai.practices import REFERENCE_PRACTICES, get_practice, practice_by_source

    print(f"✓ Successfully imported practices module")
    print(f"✓ Found {len(REFERENCE_PRACTICES)} practices in registry")

    # Verify the first practice
    first = REFERENCE_PRACTICES[0]
    print(f"✓ First practice: '{first.id}' from {first.source_repo}")
    print(f"  Title: {first.title}")

    # Test helper functions
    found = get_practice("issue-to-pr")
    print(f"✓ get_practice() works: found {found.id if found else 'None'}")

    swarm_practices = practice_by_source("openai/swarm")
    print(f"✓ practice_by_source() works: found {len(swarm_practices)} from openai/swarm")

    # Verify orchestrator can import it
    from src.hsai.orchestrator import _improvement_idea
    from src.hsai.config import load_config

    cfg = load_config(".ai-swarm/core.yaml")
    title, body = _improvement_idea(cfg)
    print(f"✓ _improvement_idea() generates: '{title}'")
    print(f"  Body preview: {body[:80]}...")

    print("\n✓ All verification checks passed!")

except Exception as e:
    print(f"✗ Verification failed: {e}")
    import traceback
    traceback.print_exc()
    exit(1)
