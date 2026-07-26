---
tags:
  - concept
  - harness
---

# hsai

**hsai** is the autonomous self-improvement loop at the center of
ai-hyperswarm-proto-core. One iteration runs in its own git worktree:

1. sync `main`, run CI, check status;
2. if red → file a P0 ticket, self-assign, heal, PR, merge on green;
3. if green with a backlog → take the top-priority ticket, implement, PR, merge;
4. if green with no backlog → explore and pick one improvement toward the
   [[Knowledge Base MOC|project goals]], file a ticket, then implement it.

Every PR it opens is linked to a ticket, records the model used, and leaves a
lesson in the knowledge base - see [[Lessons MOC]].

Related: [[Knowledge Base MOC]] · [[Whitepapers MOC]]
