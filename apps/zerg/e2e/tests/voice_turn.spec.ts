/**
 * Turn-based voice flow (mocked) - ensures UI posts audio and renders transcript/response.
 */

import { test, expect } from "./fixtures";

test("Voice turn: transcript + response appear in chat", async ({ page }) => {
  await page.addInitScript(() => {
    class MockMediaRecorder {
      static isTypeSupported() {
        return true;
      }

      public mimeType: string;
      public state: "inactive" | "recording" = "inactive";
      public ondataavailable: ((event: { data: Blob }) => void) | null = null;
      public onstop: (() => void) | null = null;

      constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
        this.mimeType = options?.mimeType || "audio/webm";
      }

      start() {
        this.state = "recording";
      }

      stop() {
        this.state = "inactive";
        if (this.ondataavailable) {
          const blob = new Blob([new Uint8Array(4096)], { type: this.mimeType });
          this.ondataavailable({ data: blob });
        }
        if (this.onstop) {
          this.onstop();
        }
      }
    }

    Object.defineProperty(window, "MediaRecorder", {
      value: MockMediaRecorder,
      configurable: true,
    });

    Object.defineProperty(navigator, "mediaDevices", {
      value: {
        getUserMedia: async () => ({
          getTracks: () => [{ stop: () => {} }],
        }),
      },
      configurable: true,
    });

    // Avoid autoplay issues in headless runs
    Object.defineProperty(HTMLMediaElement.prototype, "play", {
      value: () => Promise.resolve(),
      configurable: true,
    });
  });

  await page.route("**/api/jarvis/voice/turn", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "success",
        transcript: "Voice transcript",
        response_text: "Voice response",
        tts: null,
      }),
    });
  });

  await page.goto("/chat");
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

  const micButton = page.locator(".mic-button");
  await expect(micButton).toBeVisible();
  await expect(micButton).toHaveClass(/mic-button--ready/, { timeout: 10000 });

  await micButton.click();
  await expect(micButton).toHaveClass(/mic-button--listening/, { timeout: 10000 });
  await micButton.click();

  await expect(page.getByText("Voice transcript")).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("Voice response")).toBeVisible({ timeout: 10000 });
});
