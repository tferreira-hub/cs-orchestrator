# CS Orchestrator, 5-minute demo & video script

**Team: The Observers.** Submission structure follows the hackathon brief:
Intro+Why → Demo → Harness aspects & impact → Repo link.

---

## 0:00–0:45, Intro + Why
- "CS is fragmented across HubSpot, Zendesk, Stripe, usage, and ML churn. CSMs spend
  hours compiling data instead of acting. Revenue leaks: reactive churn, missed expansion."
- "CS Leadership just signed a **Ways of Working** framework, CSMs as **orchestrators**,
  one standardised process. We turned that framework into an **agent harness** that
  enforces it."
- One sentence: "The system originates the prioritised task queue; the CSM orchestrates."

## 0:45–1:30, Harness the power (show this slide BEFORE the demo)
Show the four-primitive table (from README):
- **Tools/MCP**, the CS stack as agent tools.
- **Custom agent**, cs-orchestrator (deterministic).
- **Skill**, the WoW rules.
- **Hook**, multi-instance suppression + grounding gate (enforcement).
- Line: "`Agent = Model + Harness`. We built one of each mechanism, encoding a *signed
  business process* as harness, so the standardised WoW is enforced by the system, not
  left to individual CSM style."

## 1:30–3:30, Demo (the money shot)
Run it live (deterministic, offline):
```bash
python3 orchestrate.py
```
Narrate the queue as it prints:
1. **P1, Northwind (MUST_PROTECT):** churn 72% + Sev-1 → Defensive Workflow, 24h SLA.
   Show the **drafted executive outreach** email. "The CSM reviews and sends, they didn't
   gather anything."
2. **P2, Initech (Day-15 payment):** Strategic, $350k ARR, 16 days past due → CSM exec
   outreach. Contrast: **SmallCo** (Scaled, 18 days past due) produced **no task**, it
   auto-suspends. "The 'No Chasing' rule, enforced: CSMs aren't debt collectors."
3. **P3, Globex & Umbrella (MUST_EXPAND):** utilization ≥ 85% → upsell.
4. **The star beat, suppression:** point to the **Suppressed** section:
   *"Umbrella Ltd: ticket spike suppressed, 16 of 18 tickets on the `um-dev` test
   instance."* "A naive alerting tool screams churn risk here. Our harness knows it's a
   test instance and keeps Umbrella an **expansion** opportunity. That's WoW §5,
   preventing false positives, enforced as a hook."
5. **Grounding gate:** show the terminal warning when a figure isn't in evidence
   (run the fabricated-number test) → "no invented churn numbers reach a CSM."

**Standardisation beat (optional, powerful):** run `orchestrate.py` twice / "two CSMs" →
identical queue. "One standardised way of working, enforced by the harness."

## 3:30–4:30, Impact + Retrospective (judged)
- **Impact:** protects revenue (Priority-1 churn, Day-15 high-ARR), captures expansion
  (utilization/API triggers), removes CSM data-gathering, enforces the signed WoW.
- **Retro (harness):**
  - MCP tools made the CS stack composable, swap fixtures for live APIs, agent unchanged.
  - Encoding rules as a **skill** + **deterministic driver** meant we could **test** the
    business logic (8 passing tests), the harness made the process verifiable.
  - The **suppression hook** is the highest-leverage piece: it turns a common false-positive
    (test-instance noise) into a non-event, deterministically.
  - What's next: bi-directional HubSpot write-back, Jiminny call sentiment tool, live data.

## 4:30–5:00, Repo + close
- Show the plugin tree (`plugins/cs-orchestrator/`), the passing tests, the branch link.
- Close: "We took a signed CS Ways of Working framework and made it a running,
  tested, standardised agent harness. Model + Harness = CSMs as orchestrators."

---

### Pre-flight checklist (so it can't flop)
- [ ] `python3 orchestrate.py` prints the expected queue (rehearsed).
- [ ] `python3 -m pytest tests/ -v` → 8 passed (show it).
- [ ] Fabricated-number grounding warning captured (record separately if needed).
- [ ] `CS_TODAY=2026-09-23` so renewal cadence (T-30/60/90/120) is stable on camera.
- [ ] Dan Hill is a judge and signed the WoW, call out that this operationalises *his* framework.
