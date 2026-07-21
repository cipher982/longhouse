import type { FullResult, Reporter, TestCase, TestResult } from "@playwright/test/reporter";

/**
 * Deliberately omits errors, stacks, steps, URLs, attachments, and console
 * payloads. The scheduled cohort test writes its typed privacy-safe artifact
 * before failing, so CI logs only need a red/green process signal.
 */
class PrivacyReporter implements Reporter {
  onTestEnd(_test: TestCase, result: TestResult): void {
    console.log(`[cohort-journey] test=${result.status}`);
  }

  onEnd(result: FullResult): void {
    console.log(`[cohort-journey] run=${result.status}`);
  }
}

export default PrivacyReporter;
