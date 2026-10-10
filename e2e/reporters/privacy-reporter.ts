import type { FullResult, Reporter, TestCase, TestResult } from "@playwright/test/reporter";
import { classifyJourneyFailure } from "../tests/live/cohort-journey-helpers";

/**
 * Deliberately omits errors, stacks, steps, URLs, attachments, and console
 * payloads. The scheduled cohort test writes its typed privacy-safe artifact
 * before failing, so CI logs only need a red/green process signal, plus one
 * bounded failure class from `classifyJourneyFailure`'s fixed set: the error
 * text is matched, never printed. Without it a CI-only failure
 * (validate-cohort-journey, 2026-10-09) left nothing to diagnose.
 */
class PrivacyReporter implements Reporter {
  onTestEnd(_test: TestCase, result: TestResult): void {
    if (result.status === "failed" || result.status === "timedOut" || result.status === "interrupted") {
      const failureClass =
        result.status === "timedOut" ? "timeout" : classifyJourneyFailure(new Error(result.error?.message ?? ""));
      console.log(`[cohort-journey] test=${result.status} class=${failureClass}`);
      return;
    }
    console.log(`[cohort-journey] test=${result.status}`);
  }

  onEnd(result: FullResult): void {
    console.log(`[cohort-journey] run=${result.status}`);
  }
}

export default PrivacyReporter;
