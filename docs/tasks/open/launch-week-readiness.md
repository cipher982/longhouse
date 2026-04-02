# Launch Week Readiness

Status: In progress
Specs: `docs/specs/launch-demo-contract.md`, `docs/specs/launch-demo-rehearsal.md`
Runbook: `docs/specs/launch-demo-rehearsal.md`
Last updated: 2026-04-02

## Goal

Ship a credible first public launch around Longhouse's real wedge:

- existing sessions become findable immediately
- new Longhouse sessions become controllable after launch
- self-hosted is the primary free path
- hosted is an honest convenience layer, not the gate

This task is about making that loop demoable, installable, and believable in one week. It is not a generic product-polish bucket.

## Done when

- landing, README, docs, installer, and onboarding all tell the same launch story
- the canonical 3-minute demo is rehearsed and proves findable-first then controllable-second
- a clean self-hosted install reaches first useful session quickly
- Claude control-after-launch is validated on a real self-hosted Longhouse session
- hosted copy is explicit about what is and is not ready
- launch-day validation steps are frozen before push/deploy

## Checklist

- [x] Lock the launch story and public vocabulary
- [x] Lock the canonical demo contract
- [x] Align installer, onboarding, docs, and landing to the same import-first activation path
- [x] Freeze hosted beta copy and CTA behavior
- [ ] Rehearse the canonical 3-minute demo end to end
- [x] Run a clean-machine or clean-home smoke of install -> import -> inspect
- [x] Validate the Claude control-after-launch proof on a real Longhouse session
- [ ] Freeze the prelaunch validation gate (`make test-ci`, `make test-e2e`, any extra smoke)
- [ ] Ship and verify the launch lane

## Work Order

Do the remaining week in this order:

1. **Demo truth**
   - rehearse the exact install -> import -> find -> wall -> control loop
   - treat any failure here as a product gap before calling it a copy problem
2. **Install and onboarding polish**
   - make the import-first path obvious
   - remove any confusion around demo mode versus real import
   - keep wrapper mode out of the launch path unless it fixes a real activation issue
3. **Public surface cleanup**
   - tighten README, landing, docs, and onboarding to the same story
   - only fix drift that affects the demo or first-run understanding
4. **Targeted feature cleanup**
   - only do code design or product cleanup when it blocks the demo or onboarding loop
   - no broad architecture refactors during launch week
5. **Validation and ship**
   - freeze the exact prelaunch test gate
   - push/deploy only after the demo loop and launch truth are stable

## Notes

- If something conflicts with the launch loop, cut it from the hero before trying to polish it.
- Wrapper mode stays out of scope for launch unless it becomes the cleanest fix to a real activation problem.
- Provider parity is not a launch requirement. Claude is the proof path.
- Oikos can support the story, but it should not become the story.
- 2026-04-02 clean-home smoke passed with a temp HOME using the current repo as the install source:
  installer, `longhouse --version`, localhost auto-auth, isolated hook install, real session import, session listing, and wall query all worked.
- That smoke surfaced and fixed one real launch polish bug: the root CLI was missing `--version`.
- Claude managed-session proof now has a clearer boundary:
  - control proof = turn accepted plus Claude hook phases observed (`thinking` / terminal)
  - durability proof = prompt also lands in transcript DB exactly once
- The managed-local Claude stress harness now supports `--verification-mode control` for launch-week proof work. This matters in synthetic temp-HOME lanes where Claude can still resolve the real `~/.claude` tree for transcript shipping while hook-driven control stays correctly bound to the temporary Longhouse instance.
- 2026-04-02 control proof passed against the live temp self-host lane with the new control-mode harness:
  one managed Claude session accepted a post-launch turn and Claude hook phases observed `thinking -> idle`.
