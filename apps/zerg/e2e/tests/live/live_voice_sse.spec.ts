import { test, expect } from './fixtures';

function buildWavBuffer(durationMs = 120, sampleRate = 8000): Buffer {
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

// Live prod E2E: Voice transcribe -> SSE chat -> TTS

test.describe('Prod Live Voice SSE', () => {
  test('voice transcribe + chat SSE uses message_id and returns TTS', async ({ request }) => {
    test.setTimeout(120_000);

    const audioBuffer = buildWavBuffer();
    const messageId = `live-voice-${Date.now()}`;

    const transcribeResponse = await request.post('/api/jarvis/voice/transcribe', {
      multipart: {
        audio: {
          name: 'sample.wav',
          mimeType: 'audio/wav',
          buffer: audioBuffer,
        },
        message_id: messageId,
      },
    });

    expect(transcribeResponse.ok()).toBeTruthy();
    const transcribeData = await transcribeResponse.json();
    expect(transcribeData.status).toBe('success');
    expect(transcribeData.message_id).toBe(messageId);
    expect(transcribeData.transcript?.length).toBeGreaterThan(0);

    const chatResponse = await request.post('/api/jarvis/chat', {
      data: {
        message: transcribeData.transcript,
        message_id: messageId,
      },
    });

    expect(chatResponse.ok()).toBeTruthy();
    const sseText = await chatResponse.text();
    expect(sseText).toContain('supervisor_complete');
    expect(sseText).toContain(messageId);

    const ttsResponse = await request.post('/api/jarvis/voice/tts', {
      data: {
        text: 'Voice SSE smoke test confirmation.',
        message_id: messageId,
      },
    });

    expect(ttsResponse.ok()).toBeTruthy();
    const ttsData = await ttsResponse.json();
    expect(ttsData.status).toBe('success');
    expect(ttsData.message_id).toBe(messageId);
    expect(ttsData.tts?.audio_base64?.length).toBeGreaterThan(0);
  });
});
