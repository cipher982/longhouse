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

function parseSSEEvents(sseText: string): Array<{ event: string; data: any }> {
  const events: Array<{ event: string; data: any }> = [];
  const chunks = sseText.split('\n\n');

  for (const chunk of chunks) {
    if (!chunk.trim()) continue;
    const lines = chunk.split('\n');
    let event = '';
    let data = '';
    for (const line of lines) {
      if (line.startsWith('event:')) {
        event = line.replace('event:', '').trim();
      } else if (line.startsWith('data:')) {
        data += line.replace('data:', '').trim();
      }
    }

    if (!event) continue;
    let parsed: any = data;
    try {
      parsed = data ? JSON.parse(data) : undefined;
    } catch {
      // leave as string
    }
    events.push({ event, data: parsed });
  }

  return events;
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
        return_audio: 'true',
      },
    });

    expect(response.ok()).toBeTruthy();

    const data = await response.json();

    expect(typeof data.transcript).toBe('string');
    expect(data.transcript.length).toBeGreaterThan(0);
    expect(typeof data.status).toBe('string');
    expect(data.status.length).toBeGreaterThan(0);
    expect(typeof data.response_text).toBe('string');
    expect(data.tts).toBeTruthy();
    expect(typeof data.tts.audio_base64).toBe('string');
    expect(data.tts.audio_base64.length).toBeGreaterThan(0);
  });

  test('message_id passthrough for history correlation', async ({ request }) => {
    const audioBuffer = buildWavBuffer();
    const testMessageId = 'e2e-test-uuid-' + Date.now();

    const response = await request.post('/api/jarvis/voice/turn', {
      multipart: {
        audio: {
          name: 'sample.wav',
          mimeType: 'audio/wav',
          buffer: audioBuffer,
        },
        return_audio: 'false',
        message_id: testMessageId,
      },
    });

    expect(response.ok()).toBeTruthy();

    const data = await response.json();
    expect(data.message_id).toBe(testMessageId);
    expect(data.status).toBe('success');
  });

  test('transcribe -> chat SSE uses same message_id', async ({ request }) => {
    const audioBuffer = buildWavBuffer();
    const testMessageId = 'voice-sse-' + Date.now();

    const transcribeResponse = await request.post('/api/jarvis/voice/transcribe', {
      multipart: {
        audio: {
          name: 'sample.wav',
          mimeType: 'audio/wav',
          buffer: audioBuffer,
        },
        message_id: testMessageId,
      },
    });

    expect(transcribeResponse.ok()).toBeTruthy();
    const transcribeData = await transcribeResponse.json();
    expect(transcribeData.status).toBe('success');
    expect(transcribeData.transcript?.length).toBeGreaterThan(0);
    expect(transcribeData.message_id).toBe(testMessageId);

    const chatResponse = await request.post('/api/jarvis/chat', {
      data: {
        message: transcribeData.transcript,
        message_id: testMessageId,
      },
    });

    expect(chatResponse.ok()).toBeTruthy();
    const sseText = await chatResponse.text();
    const events = parseSSEEvents(sseText);
    const completeEvent = events.find((evt) => evt.event === 'supervisor_complete');
    expect(completeEvent).toBeTruthy();
    const payload = completeEvent?.data?.payload ?? completeEvent?.data;
    expect(payload?.message_id || payload?.messageId).toBe(testMessageId);
  });
});
