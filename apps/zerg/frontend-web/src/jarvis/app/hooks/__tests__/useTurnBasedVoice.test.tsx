import React from "react";
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { useTurnBasedVoice } from "../useTurnBasedVoice";
import { AppProvider, useAppDispatch, useAppState } from "../../context";
import * as api from "../../../../services/api";

let mockVoiceTranscribe: ReturnType<typeof vi.spyOn>;
let mockVoiceTts: ReturnType<typeof vi.spyOn>;

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
      this.ondataavailable({
        data: new Blob([new Uint8Array(4096)], { type: this.mimeType }),
      });
    }
    if (this.onstop) {
      this.onstop();
    }
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  return <AppProvider>{children}</AppProvider>;
}

function useHarness(sendText?: (text: string, messageId: string) => Promise<void>) {
  const voice = useTurnBasedVoice({ sendText });
  const state = useAppState();
  const dispatch = useAppDispatch();
  return { voice, state, dispatch };
}

describe("useTurnBasedVoice", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockVoiceTranscribe = vi.spyOn(api, "voiceTranscribe");
    mockVoiceTts = vi.spyOn(api, "voiceTts");

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

  it("records audio, transcribes, and sends transcript via SSE", async () => {
    const sendText = vi.fn().mockResolvedValue(undefined);
    mockVoiceTranscribe.mockResolvedValue({
      status: "success",
      transcript: "Hello from voice",
    });

    const { result } = renderHook(() => useHarness(sendText), { wrapper });

    await act(async () => {
      await result.current.voice.startRecording();
    });

    await act(async () => {
      result.current.voice.stopRecording();
    });

    await waitFor(() => {
      const userMessage = result.current.state.messages.find((msg) => msg.role === "user");
      expect(mockVoiceTranscribe).toHaveBeenCalledTimes(1);
      expect(sendText).toHaveBeenCalledTimes(1);
      expect(userMessage?.content).toBe("Hello from voice");
    });
  });

  it("requests TTS when assistant message is finalized", async () => {
    const sendText = vi.fn().mockResolvedValue(undefined);
    mockVoiceTranscribe.mockResolvedValue({
      status: "success",
      transcript: "Hello from voice",
    });
    mockVoiceTts.mockResolvedValue({
      status: "error",
      error: "tts unavailable",
    });

    const { result } = renderHook(() => useHarness(sendText), { wrapper });

    await act(async () => {
      await result.current.voice.startRecording();
    });

    await act(async () => {
      result.current.voice.stopRecording();
    });

    await waitFor(() => {
      expect(sendText).toHaveBeenCalledTimes(1);
    });

    const assistant = result.current.state.messages.find((msg) => msg.role === "assistant" && msg.messageId);
    expect(assistant?.messageId).toBeTruthy();

    await act(async () => {
      result.current.dispatch({
        type: "UPDATE_MESSAGE_BY_MESSAGE_ID",
        messageId: assistant!.messageId!,
        updates: { content: "Final response", status: "final", timestamp: new Date() },
      });
    });

    await waitFor(() => {
      expect(mockVoiceTts).toHaveBeenCalledWith({
        text: "Final response",
        message_id: assistant!.messageId,
      });
    });
  });
});
