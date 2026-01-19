import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import { AppProvider } from '../../context/AppContext';
import { useVoice } from '../useVoice';

const mockVoiceController = vi.hoisted(() => ({
  addListener: vi.fn(),
  removeListener: vi.fn(),
  setMicrophoneStream: vi.fn(),
  startPTT: vi.fn(),
  stopPTT: vi.fn(),
  isConnected: vi.fn().mockReturnValue(true),
  getState: vi.fn().mockReturnValue({ handsFree: false }),
}));

vi.mock('../../../lib/voice-controller', () => ({
  voiceController: mockVoiceController,
}));

function wrapper({ children }: { children: React.ReactNode }) {
  return <AppProvider>{children}</AppProvider>;
}

describe('useVoice', () => {
  beforeEach(() => {
    mockVoiceController.addListener.mockClear();
    mockVoiceController.removeListener.mockClear();
    mockVoiceController.setMicrophoneStream.mockClear();
    mockVoiceController.startPTT.mockClear();
    mockVoiceController.stopPTT.mockClear();
    mockVoiceController.isConnected.mockReturnValue(true);

    const fakeStream = { getTracks: () => [] } as MediaStream;

    Object.defineProperty(globalThis.navigator, 'mediaDevices', {
      value: {
        getUserMedia: vi.fn().mockResolvedValue(fakeStream),
      },
      configurable: true,
    });
  });

  it('starts listening via voiceController and attaches mic stream', async () => {
    const { result } = renderHook(() => useVoice(), { wrapper });

    await act(async () => {
      await result.current.startListening();
    });

    expect(mockVoiceController.setMicrophoneStream).toHaveBeenCalledTimes(1);
    expect(mockVoiceController.startPTT).toHaveBeenCalledTimes(1);
  });

  it('stops listening via voiceController', async () => {
    const { result } = renderHook(() => useVoice(), { wrapper });

    await act(async () => {
      result.current.stopListening();
    });

    expect(mockVoiceController.stopPTT).toHaveBeenCalledTimes(1);
  });
});
