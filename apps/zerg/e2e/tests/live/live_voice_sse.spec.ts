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
  test('voice transcribe + chat SSE uses message_id (TTS audio as input)', async ({ request }) => {
    test.setTimeout(120_000);

    const messageId = `live-voice-${Date.now()}`;

    // Use TTS to generate real speech audio for reliable STT in prod.
    const ttsResponse = await request.post('/api/jarvis/voice/tts', {
      data: {
        text: 'Hello from the live voice test.',
        message_id: messageId,
      },
    });

    expect(ttsResponse.ok()).toBeTruthy();
    const ttsData = await ttsResponse.json();
    expect(ttsData.status).toBe('success');
    expect(ttsData.message_id).toBe(messageId);
    expect(ttsData.tts?.audio_base64?.length).toBeGreaterThan(0);

    const ttsContentType = ttsData.tts?.content_type || 'audio/mpeg';
    const audioBuffer = Buffer.from(ttsData.tts.audio_base64, 'base64');

    const transcribeResponse = await request.post('/api/jarvis/voice/transcribe', {
      multipart: {
        audio: {
          name: 'sample.mp3',
          mimeType: ttsContentType,
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

    // No extra TTS step needed here; we already validated TTS output above.
  });
});
