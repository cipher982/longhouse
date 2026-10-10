import { afterEach, describe, expect, test } from "bun:test";
import PrivacyReporter from "./privacy-reporter";

const originalLog = console.log;

afterEach(() => {
  console.log = originalLog;
});

describe("privacy reporter", () => {
  test("never prints Playwright error details", () => {
    const output: string[] = [];
    console.log = (...values: unknown[]) => output.push(values.map(String).join(" "));
    const reporter = new PrivacyReporter();

    reporter.onTestEnd?.({} as never, {
      status: "failed",
      error: { message: "private-query /timeline/secret-session" },
    } as never);

    expect(output).toEqual(["[cohort-journey] test=failed class=browser_or_contract_failure"]);
    expect(output.join(" ")).not.toContain("private-query");
    expect(output.join(" ")).not.toContain("secret-session");
  });

  test("names a known failure class without the message around it", () => {
    const output: string[] = [];
    console.log = (...values: unknown[]) => output.push(values.map(String).join(" "));
    const reporter = new PrivacyReporter();

    reporter.onTestEnd?.({} as never, {
      status: "failed",
      error: { message: "paint_evidence_unavailable for /timeline/secret-session" },
    } as never);
    reporter.onTestEnd?.({} as never, { status: "timedOut" } as never);
    reporter.onTestEnd?.({} as never, { status: "passed" } as never);

    expect(output).toEqual([
      "[cohort-journey] test=failed class=paint_evidence_unavailable",
      "[cohort-journey] test=timedOut class=timeout",
      "[cohort-journey] test=passed",
    ]);
    expect(output.join(" ")).not.toContain("secret-session");
  });
});
