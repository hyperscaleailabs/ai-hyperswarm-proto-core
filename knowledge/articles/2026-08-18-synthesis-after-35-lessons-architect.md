---
tags:
  - article
  - persona/architect
---

# Lessons 32–35: Governance Consolidation and the Model-Routing Inflection

At lesson 35, you've crossed another threshold. Governance is now a repeatable rhythm. But feature complexity has hit a wall — and that wall is telling you something important about how to scale the loop.

## The chain of events

**Lessons 32–33** (governance artifacts for blocks 41361 and 41363): Both passed on the first try with haiku. The governance loop is now a reliable, repeatable process. Whitepaper + persona articles + MOC reindex + DIRECTION refresh: these are no longer one-off, they're a **standard rhythm**.

**Lesson 34** (adopted-practice registry with provenance): Timed out at 1200s with sonnet. This was supposed to wire adopted practices back into synthesis, so the loop stops re-proposing ideas it already tried. It's a governance-infrastructure play, not a feature. It's important. And it needs more resources than sonnet can deliver.

**Lesson 35** (failure taxonomy in the ledger): Also timed out at 1200s with sonnet. This was supposed to add structured failure analysis, so the loop learns patterns from its own failures. Critical infrastructure. Same timeout wall.

## What this means for your architecture

Most autonomous systems hit this inflection point and do one of three things:
- **Option A**: Retry with the same model (waste quota, same timeout)
- **Option B**: Give up on the feature (accept the gap in the system)
- **Option C**: Escalate to a heavier model or decompose the work (scale intelligently)

You're at option C.

The pattern is now clear: **governance-touching features (both in code and in the knowledge base) consistently need opus, not sonnet.** 

Lessons 34–35 aren't failures in the traditional sense — CI passed in both cases. They're **scheduling signals**. The loop is telling you: "This work is too complex for this model tier. Give me a bigger engine, or break it into smaller pieces."

## The model-routing decision

You have three paths forward:

### Path 1: Simple Heuristic (fast, low-risk)
Implement a routing rule: if a ticket is a feature (`feat:` kind) **and** touches `governance/` or `knowledge/` directories, default to `opus` instead of `sonnet`.

**Pros**: Takes 1–2 hours, immediately unblocks lessons 34–35, clear and auditable.  
**Cons**: Costs more quota per such ticket, doesn't handle edge cases or future unknowns.  
**Timeline**: Implement in lesson 36, unblock 34–35 retries in lesson 37–38.

### Path 2: Learned Heuristic (scalable, requires data)
This is issue #42 on your backlog. Build a model-selection classifier that learns which tickets need which tier from past lesson outcomes. Aggregate signals: ticket complexity, file-set size, estimated synthesis burden, model tier used, timeout yes/no.

**Pros**: Scales to all ticket types, learns from experience, can be tuned as new patterns emerge.  
**Cons**: Takes 3–4 lessons to collect enough data, requires metric definition, risky if tuned wrong.  
**Timeline**: Start in lesson 36–37, have v1 ready by lesson 41–42.

### Path 3: Decomposition First (conservative, most effort)
Take issue #272 and #273 (the blocked tickets that drove lessons 34–35) and break them into smaller sub-tasks. Let sonnet handle smaller pieces, opus handle orchestration, haiku handle the drudge work.

**Pros**: Doesn't increase quota costs per ticket, teaches the loop to decompose complex work.  
**Cons**: Requires re-architecting both tickets, takes 2–3 lessons per ticket.  
**Timeline**: Start in lesson 36, finish by lesson 39–40.

## My recommendation

**Implement Path 1 + start Path 2 data collection in parallel:**

- **Lesson 36**: Wire in the simple heuristic (governance-touching features → opus)
- **Lesson 37–38**: Retry lessons 34–35 with opus; both should pass
- **Lesson 39+**: Start tuning the learned heuristic (Path 2) with real data from lesson 37–38 onward

Why this sequence? Because it's the **lowest-risk fast path**. Path 1 is simple enough that you can implement it without risk, it immediately proves the hypothesis (governance features need opus), and it lets you collect the data you need for Path 2.

By lesson 41–42, you'll have:
- Two successful retries of complex features (lessons 34–35 with opus)
- Real cost/benefit data on when opus was necessary
- A pipeline to build the learned heuristic

After that, you can decide whether to finalize Path 2 or double down on Path 3 (decomposition).

## The bigger picture: self-scaling infrastructure

Lessons 32–35 show a loop that's maturing in real-time. It's no longer just **executing** — it's now **introspecting**. It recognizes its own limits and stops to think.

What that requires is not just governance (which is now solid) or execution (which works). What it requires is **intelligent routing**. The loop needs to look at a task and decide: "This is a small haiku task," or "This needs sonnet," or "This is a big opus play."

Implementing model routing — whether via simple heuristics (Path 1) or learned classifiers (Path 2) — is the gateway to **self-scaling**. After that, the loop can handle anything by adjusting its own resource allocation, not by timing out and asking for help.

That's the next frontier. Lessons 34–35 are your signal that you're ready for it.

## What's fragile

Two things:
1. **You're still reactive on model selection.** Lesson 34 and 35 had to timeout before you noticed they needed opus. A mature system predicts this.
2. **The knowledge loop isn't closed.** Lessons 34–35 were infrastructure plays (adopted practices, failure analysis). Until they land, the loop is still learning without systematizing what it learns.

## The path forward

Block 41365 is decision time — similar to the escalation decision at block 41363:
- Decide on model routing (Path 1 is my recommendation)
- File a ticket to implement it
- Watch lessons 37+ handle complex features without timeout waste

After that, the loop will be capable of self-scaling. You'll have closed the biggest gap in the execution layer — knowing when to ask for more horsepower, before the wall hits.

## Next question

You've now answered:
- Lesson 1–15: Can the loop work? (Yes)
- Lesson 15–25: Can it self-modify? (Yes)
- Lesson 25–32: Can it repair its own governance? (Yes)
- Lesson 32–35: Does it understand when it's out of its depth? (Yes)
- **Lesson 36–40: Can it route itself to the right resources?** (That's yours to answer)

That last one is yours to architect.
