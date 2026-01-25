import React from "react";
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { AppProvider } from "../../context/AppContext";
import { useTurnBasedVoice } from "../useTurnBasedVoice";
import { useAppState } from "../../context";

const mockVoiceTurn = vi.fn();

vi.mock("../../../services/api", () => ({
  voiceTurn: (...args: unknown[]) => mockVoiceTurn(...args),
}));

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
      this.ondataavailable({ data: new Blob(["test"], { type: this.mimeType }) });
    }
    if (this.onstop) {
      this.onstop();
    }
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  return <AppProvider>{children}</AppProvider>;
}

function useHarness() {
  const voice = useTurnBasedVoice();
  const state = useAppState();
  return { voice, state };
}

describe("useTurnBasedVoice", () => {
  beforeEach(() => {
    mockVoiceTurn.mockReset();

    Object.defineProperty(globalThis, "MediaRecorder", {
      value: MockMediaRecorder,
      configurable: true,
    });

    const fakeStream = {
      getTracks: () => [{ stop: vi.fn() }],
    } as unknown as MediaStream;

    Object.defineProperty(globalThis.navigator, "mediaDevices", {
      value: {
        getUserMedia: vi.fn().mockResolvedValue(fakeStream),
      },
      configurable: true,
    });
  });

  it("records audio and sends voice turn", async () => {
    mockVoiceTurn.mockResolvedValue({
      status: "success",
      transcript: "Hello from voice",
      response_text: "Hi there",
      tts: null,
    });

    const { result } = renderHook(() => useHarness(), { wrapper });

    await act(async () => {
      await result.current.voice.startRecording();
    });

    await act(async () => {
      result.current.voice.stopRecording();
    });

    await waitFor(() => {
      expect(mockVoiceTurn).toHaveBeenCalledTimes(1);
      expect(result.current.state.messages).toHaveLength(2);
    });

    const [userMessage, assistantMessage] = result.current.state.messages;
    expect(userMessage.content).toBe("Hello from voice");
    expect(assistantMessage.content).toBe("Hi there");
  });
});
