/**
 * Voice Turn-Based Tests - Core Suite
 *
 * Validates /api/jarvis/voice/turn using a tiny WAV payload.
 */

import { test, expect } from '../fixtures';
import { resetDatabase } from '../test-utils';

function buildWavBuffer(durationMs = 100, sampleRate = 8000): Buffer {
  const numSamples = Math.floor((sampleRate * durationMs) / 1000);
  const dataSize = numSamples * 2; // 16-bit mono
  const buffer = Buffer.alloc(44 + dataSize);

  buffer.write('RIFF', 0);
  buffer.writeUInt32LE(36 + dataSize, 4);
  buffer.write('WAVE', 8);

  buffer.write('fmt ', 12);
  buffer.writeUInt32LE(16, 16); // PCM fmt chunk size
  buffer.writeUInt16LE(1, 20); // audio format PCM
  buffer.writeUInt16LE(1, 22); // channels
  buffer.writeUInt32LE(sampleRate, 24);
  buffer.writeUInt32LE(sampleRate * 2, 28); // byte rate
  buffer.writeUInt16LE(2, 32); // block align
  buffer.writeUInt16LE(16, 34); // bits per sample

  buffer.write('data', 36);
  buffer.writeUInt32LE(dataSize, 40);

  // data section is already zeroed (silence)
  return buffer;
}

test.beforeEach(async ({ request }) => {
  await resetDatabase(request);
});

test.describe('Voice Turn-Based - Core', () => {
  test('transcribe + supervisor response returns payload', async ({ request }) => {
    const audioBuffer = buildWavBuffer();

    const response = await request.post('/api/jarvis/voice/turn', {
      multipart: {
        audio: {
          name: 'sample.wav',
          mimeType: 'audio/wav',
          buffer: audioBuffer,
        },
      },
    });

    expect(response.ok()).toBeTruthy();

    const data = await response.json();

    expect(typeof data.transcript).toBe('string');
    expect(data.transcript.length).toBeGreaterThan(0);
    expect(typeof data.status).toBe('string');
    expect(data.status.length).toBeGreaterThan(0);
    expect(typeof data.response_text).toBe('string');
  });
});
