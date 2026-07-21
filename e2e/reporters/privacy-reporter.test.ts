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

    expect(output).toEqual(["[cohort-journey] test=failed"]);
    expect(output.join(" ")).not.toContain("private-query");
    expect(output.join(" ")).not.toContain("secret-session");
  });
});
