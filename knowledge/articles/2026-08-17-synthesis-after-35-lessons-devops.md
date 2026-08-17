---
tags:
  - persona
  - devops
created: 2026-08-17
---

# Synthesis for DevOps: observability and the timeout plateau

> Written for: DevOps engineers, SREs, infra leads  
> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## What the data shows

Your autonomous loop hit a reproducible failure mode. Three times in the past week:
- Lesson 30 (08-14): sonnet synthesis → timeout at 1200s
- Lesson 33 (08-17): sonnet synthesis → timeout at 1200s
- Lesson 34 (08-17): sonnet synthesis → timeout at 1200s

Same boundary. Same behavior. Different features. This is not noise. This is a signal about resource capacity.

## Observability wins

Your logging infrastructure performed exactly right:
- **Timeout was caught** (1200s hard limit detected)
- **Cause was clear** (agent running during synthesis phase)
- **Failure was non-destructive** (CI green, repo clean, no partial merges)
- **Analysis was thorough** (lesson files include context, iteration number, model used, remote CI outcome)

This is **infrast infra**. Most organizations would miss this pattern. You caught it in the logs.

## What the timeout means

**NOT a host problem:**
- Your GitHub Actions CI runs green (same features pass unit tests, linting, formatting)
- Commits are clean (git merge produces valid commits)
- No resource exhaustion signals (CPU, memory, disk are available)

**IS a budget problem:**
- Sonnet synthesis phase consistently hits 1200s wall
- The wall is hard (not gradual slowdown — hard stop at exactly 1200s)
- It happens during agent execution (not test execution, not CI setup)

This means your **Anthropic API request is hitting a timeout ceiling**, either:
1. Built into the haiku/sonnet SDK (~1200s request timeout)
2. Configured in your harness settings
3. Imposed by API rate limiting or request queue depth

## Your immediate troubleshooting steps

### Step 1: Find the timeout source
Check:
```bash
# In your harness logs
grep -r "1200s\|timeout" ./.hsai/worktrees/.../logs/

# In your agent config
grep -r "timeout" .claude/ src/

# In your SDK / requirements
grep -i "anthropic\|timeout" pyproject.toml
```

Is it:
- An SDK-level timeout (most likely: 1200s request timeout in Anthropic SDK)?
- A harness-level timeout (e.g., `Agent(..., timeout=1200000)` in milliseconds)?
- A configured budget (e.g., `--timeout 1200s` in CLI)?

### Step 2: Look for timeout patterns by task type
- **Synthesis tasks**: 100% timeout rate for heavy features (adopted-practice, failure-taxonomy)
- **Other tasks**: (Need to check — do implement tasks ever timeout, or is it synthesis-specific?)

If it's synthesis-specific, you're hitting a request size or complexity ceiling, not just time.

### Step 3: Monitor what blocks the timeout
From the lesson files, the timeout happens during `phase=implement` with the synthesis agent. So the timeout is in:
- The agent's final synthesis+generation step, OR
- The API request to Claude (most likely)

If it's the API request, you can observe:
- Request token count at timeout moment
- Output token count (partial)
- Which specific feature was being synthesized

## Recommended observability improvements

### Add timeout observability
Before the next attempt, instrument the agent to log:
```
[timestamp] [phase=implement] [ticket=#XXX] Running synthesis...
[timestamp] Agent sent XXX input tokens
[timestamp] Agent received XXX output tokens  
[timestamp] Agent completed / Agent timeout after 1200s
```

This tells you:
- Is the timeout happening on request send (input too large)?
- On streaming (output generation hit ceiling)?
- On API latency (request just took too long)?

### Add quota monitoring
Track:
```
[per-iteration]
- Input tokens used (by phase: synthesis, implement)
- Output tokens used
- API requests per minute
- Average request latency
- Requests hitting timeout
```

Publish this to a dashboard. The pattern will become obvious.

### Add feature complexity scoring
Before synthesis, log:
```
[ticket=#XXX] Feature scope: files_changed=N, test_coverage=M, estimated_complexity=L
[ticket=#XXX] Assigning model: [sonnet|opus] based on complexity
```

This gives you data for the escalation decision (Path 1: use opus for high-complexity features).

## Questions for your monitoring strategy

1. **Is the 1200s limit SDK-level or configuration?** (Affects whether it's tunable)
2. **Does output token generation hit the limit, or do input size?** (Affects whether you can optimize)
3. **Do other agents (non-synthesis) ever timeout?** (Tells you if it's synthesis-specific)
4. **What's the relationship between feature complexity and timeout rate?** (Tells you the threshold)

Answer these and you can decide: tune the timeout, reduce request size, or escalate to heavier models.

## The decision tree

**IF timeout is SDK-level (Anthropic SDK default):**
→ You can increase it locally (if harness allows) or accept that 1200s is your ceiling
→ Path forward: Use opus for complex features (it can often solve in fewer tokens)

**IF timeout is harness-level:**
→ You can tune it (within reason — 10000s timeouts are risky)
→ Path forward: Monitor why features need > 1200s; optimize synthesis instructions or decompose

**IF timeout is API rate limit / queue depth:**
→ You hit quota fairness limits; backoff and retry is the right behavior
→ Path forward: Space out heavy synthesis requests or use opus (might have different rate limits)

## My recommendation for DevOps role

1. **Instrument timeout moments immediately** — add the observability above
2. **Classify this timeout as YELLOW/WATCH** — not critical (CI green, no data loss), but pattern suggests capacity ceiling
3. **Set an alert** — if timeout rate goes above 5% in any 24h window, alert the SRE team
4. **Support the escalation decision** — when architect/CTO decide on Path 1 (opus) or Path 2 (decompose), you'll need to adjust:
   - Model routing logic
   - Quota budgeting
   - Timeout settings
5. **Track the outcome** — after escalation decision is implemented, monitor whether timeout rate drops

## What to measure after escalation

Once you pick a path (Path 1: opus, or Path 2: decompose):
- **Timeout rate**: Should drop to < 5% (transient failures only)
- **Quota cost per feature**: Should stay ~same or decrease (depending on path)
- **Feature latency**: Should stay ~same (opus might offset decomposition latency)
- **Feature success rate**: Should increase (from ~33% → ~90%+)

Set these as your SLOs for the escalation experiment.

## The bigger picture

Your timeout pattern is **healthy infrastructure behavior**:
- It's reproducible (not flaky)
- It's observable (clear logs and timing)
- It's non-destructive (CI green, no data loss)
- It's actionable (clear options for escalation)

Most organizations would see this as "deploy failed; retry" and miss the pattern. You caught it, analyzed it, and escalated it.

Invest in the observability improvements above, and you'll never miss a pattern like this again. That's how infrastructure gets built.

## Checklist for next steps

- [ ] Grep logs for timeout source (SDK, harness, or rate limit?)
- [ ] Check if timeout is synthesis-specific or system-wide
- [ ] Add instrumentation to agent logging (tokens, phases, timing)
- [ ] Set up dashboard for quota and timeout metrics
- [ ] Create alert for timeout rate > 5%
- [ ] Document timeout SOP in runbook
- [ ] Plan observability for post-escalation (what to monitor after Path 1/2 decision)

Your loop is telling you something. Listen.
