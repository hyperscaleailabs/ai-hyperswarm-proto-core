---
tags:
  - article
  - persona/devops
---

# Lessons 32–36: Capacity Planning and the Timeout Pattern

At lesson 36, the loop has established a clear failure pattern: large features time out at 1200s, even with model escalation. From a DevOps perspective, this is a resource allocation problem.

## The Pattern

| Lesson | Model | Timeout | CI Result | Trend |
| --- | --- | --- | --- | --- |
| 30 | sonnet | 1200s | PASS | Single timeout |
| 34 | sonnet | 1200s | PASS | Pattern begins |
| 35 | sonnet | 1200s | PASS | Pattern repeats |
| 36 | opus | 1200s | PASS | Pattern persists (escalation fails) |

CI passes in all cases. This is critical: the agents are not crashing or producing bad code. They're running out of time.

## What Happens When an Agent Times Out

1. **Thinking phase** (600s): Agent plans the work
2. **Execution phase** (300s): Agent writes code, runs local tests
3. **CI phase** (300s offsite): Remote CI runs ruff + pytest
4. **At 1200s total**: Agent is killed mid-run

The CI eventually passes (after human intervention or retry), but the agent never sees the result.

## The Quota Impact

Each timeout costs:
- **Token cost**: Full sonnet/opus run (~50k–200k tokens depending on model)
- **Wall-clock cost**: 1200s of harness resources
- **Opportunity cost**: Blocked ticket, can't proceed to next lesson

At lessons 30, 34, 35, 36 (4 timeouts), you've spent significant quota with no forward progress on those specific tickets.

## The Escalation That Didn't Work

In lesson 36, you escalated to opus. This is the right instinct for "the task is too complex." But it didn't help. Opus still timed out at 1200s.

This tells us:
- **It's not thinking time** — Opus thinks faster than sonnet, but still timed out
- **It's likely I/O or code generation time** — Reading files, writing code, running tests take similar time regardless of model
- **The task is genuinely large** — Even with a better model, the scope exceeds 1200s

## Resource Allocation Options

### Option A: Increase Timeout (Scaling Up)
Set a higher wall-clock limit (e.g., 2400s or 3600s).

**Pros**:
- Simple configuration change
- Gives large tasks more time
- No code changes needed

**Cons**:
- Higher resource cost per agent run
- Longer feedback loop for workers
- May not solve the underlying problem (task might need 5000s)
- Quota cost increases linearly

**Risk**: Masking a decomposition problem. If a task needs 3600s, it's probably too large and should be broken down.

### Option B: Optimize Execution (Scaling Smarter)
Reduce the time spent in each phase without changing the task.

**Possible optimizations**:
- **Faster file I/O**: Use caching or memory-mapped files
- **Smarter test selection**: Run only affected tests, not full suite
- **Parallel execution**: Run some steps in parallel (CI already does this)
- **Incremental compilation**: Cache build artifacts between runs

**Pros**:
- No quota increase
- Faster feedback for all tasks
- Architectural improvement

**Cons**:
- Requires engineering work (2–4 weeks)
- Risk of regressing test coverage
- May only save 200–300s per run

**Realistic impact**: Maybe 15–20% faster, not 2–3x.

### Option C: Decompose Tasks (Scaling Differently)
Break large tasks into smaller ones, run them sequentially across lessons.

**Pros**:
- Scales to arbitrarily complex features
- No quota increase (actually cheaper overall)
- Each subtask runs faster
- Builds dependency tracking (valuable for orchestration)

**Cons**:
- Requires synthesis to decompose well
- Higher failure rate if decomposition is wrong
- Takes 2–3 weeks to build

**Realistic impact**: Solves lessons 34–36 and all future large tasks.

## My Recommendation

**Implement both A and C in parallel**:

1. **This week** (Option A): Increase timeout from 1200s to 1800s (50% increase).
   - Not a long-term solution, but it reduces pressure while you build C
   - Cost: ~50% quota increase for this window only
   - Benefit: Might allow lesson 36 to succeed as-is

2. **Next 2–3 weeks** (Option C): Build task decomposition.
   - Start with manual decomposition (architect proposes subtasks)
   - Analyze the pattern from lessons 34–36
   - Build automatic decomposition heuristic
   - Test on new large features

3. **After decomposition works**: Drop the timeout increase.
   - Revert to 1200s
   - Quota cost returns to baseline
   - Loop can handle arbitrarily complex features

## The CI Opportunity

One thing I notice: CI passes in all timeout cases. This means:
- The code being written is correct
- The tests cover it
- The problem is purely execution time

This is actually good news. You have a resource problem, not a quality problem.

**Optimization opportunity**: Could you run a subset of tests during the agent run (say, 30s timeout for quick feedback) and then run the full suite later? This might let agents complete their work and hand off to CI for comprehensive testing.

Trade-off: Agents might miss failures that would show up in the full test suite. Mitigate with mandatory pre-merge CI gating (which you already have).

## Capacity Planning Going Forward

Here's what I'd track:

| Metric | Baseline | Target | Note |
| --- | --- | --- | --- |
| Agent run time (p50) | 300s | 400s | Will increase with more complex features |
| Agent run time (p99) | 1200s | 2400s | For large features during decomposition |
| Timeout rate | 10% | < 2% | Once decomposition is in place |
| Quota per lesson | Trending up | Flat | Decomposition should keep cost constant |

If timeout rate stays > 5% after implementing decomposition, that's a signal to revisit the heuristic.

## The Bottleneck Stack

From fastest to slowest:

1. **Model inference** — Opus is ~2x slower than sonnet on wall-clock (but used rarely for large tasks)
2. **Code generation** — Agents writing 300+ SLOC takes ~100s
3. **Test execution** — Running full pytest suite can take 150–300s
4. **CI integration** — Pushing to GitHub and waiting for remote CI adds 150–300s
5. **File I/O** — Reading/writing large files or codebases adds overhead

Decomposition helps by reducing code generation + test execution per run.

## Long-term: Resource Usage by Lesson Kind

Track this over the next 10 lessons:

- **Chores** (governance): Usually < 300s, uses haiku
- **Bug fixes**: Usually 300–600s, uses sonnet
- **Features (small)**: 600–900s, uses sonnet
- **Features (large)**: Currently > 1200s, trying opus (not working)
- **Features (huge)**: Unknown, probably needs decomposition first

This data will guide your decomposition heuristic.

## The Measurement You Need

To build a decomposition strategy, measure each lesson:
- Lines of code written
- Files modified
- Test lines added
- Time spent in planning vs. coding vs. testing

This becomes your training data for the heuristic: "if lines > X or files > Y, decompose."

## Bottom Line

You have a capacity problem, not a quality problem. The system is healthy — it just needs better resource allocation. Decomposition is the right long-term move. A temporary timeout increase (1200s → 1800s) buys you time to build it without blocking lessons 34–36.

Implement both. Monitor the metrics. Adjust as needed.
