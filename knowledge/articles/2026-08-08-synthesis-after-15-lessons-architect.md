---
tags:
  - article
  - persona/architect
---

# Synthesis After 15 Lessons: What This Build Loop Taught Us

Five lessons back-to-back, two `implement` and three `improve`, all green. That streak is worth examining not because it proves the system works, but because it's rare enough in this project's history to be a signal — the boring stretch after the hard problems got fixed.

**The pattern that got adopted: complexity-based model selection.** Instead of routing every task through the same model tier, the skill layer now inspects task complexity before dispatch and picks accordingly. The tradeoff is honest: this adds a classification step and a failure mode (misjudging complexity routes cheap tasks to expensive models or vice versa), but it beats the alternative we tried first — a fixed tier per task *kind* — which consistently over-provisioned simple `improve` tickets and under-provisioned `implement` tickets that turned out to be architecturally deep. Kind alone wasn't a good enough proxy for cost/quality tradeoff; we needed a signal closer to actual work.

**What failed before this window, and why it shows up now as infrastructure:** the "reproduce-before-fix" regression guard and the fake-runner integration tests exist because earlier lessons burned cycles on fixes that didn't reproduce the bug being fixed — passing CI while leaving the real defect in place. The fix wasn't more review, it was structural: force reproduction as a gate before any fix ticket can proceed, and back it with integration tests against a fake runner that exercises `run-once`, `heal`, and `implement` paths without needing the full orchestrator live. This is a case where process debt from failures several windows back became the load-bearing beam of this one — the clean run here is downstream of that earlier pain, not independent of it.

**Loop reliability and CI parity** is the other quiet win with a rougher backstory: retries were previously masking real CI failures by re-running until green, which is a classic trap — it optimizes for the metric (pass rate) while eroding the thing the metric was supposed to protect (actual correctness). The adopted pattern narrows retry scope to known-flaky infrastructure failures and requires CI parity checks so local-green doesn't diverge from CI-green.

**Explicit phase artifacts (MetaGPT-influenced)** and the governance/budget-gate work (quota ledger, warn-then-halt per block) are the two structural bets still unproven at scale. Both trade upfront ceremony — writing artifacts, tracking spend — for auditability and blast-radius control. Neither has yet been stress-tested by a failure in this window, which is the honest caveat here: five passes is evidence the guardrails aren't actively broken, not evidence they'll hold under a harder ticket.

**The tradeoff worth naming for other architects:** every guard added here (reproduce-before-fix, budget gate, CI parity) trades iteration speed for a class of failure eliminated. That's the right trade when the failure class is expensive (silent regressions, cost overrun) and wrong when it isn't — so the real discipline is in choosing which failures earn a permanent gate versus a lesson learned and moved past.
