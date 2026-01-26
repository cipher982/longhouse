import { request } from "./base";

export interface VoiceAudioPayload {
  audio_base64: string;
  content_type: string;
  provider?: string | null;
  latency_ms?: number | null;
  error?: string | null;
  truncated?: boolean;
}

export interface VoiceTurnResponse {
  transcript: string;
  response_text?: string | null;
  status: string;
  run_id?: number | null;
  thread_id?: number | null;
  error?: string | null;
  stt_model?: string | null;
  tts?: VoiceAudioPayload | null;
  message_id?: string | null;
}

export interface VoiceTranscribeResponse {
  transcript: string;
  status: string;
  error?: string | null;
  stt_model?: string | null;
  message_id?: string | null;
}

export interface VoiceTtsRequest {
  text: string;
  provider?: string | null;
  voice_id?: string | null;
  message_id?: string | null;
}

export interface VoiceTtsResponse {
  status: string;
  tts?: VoiceAudioPayload | null;
  error?: string | null;
  message_id?: string | null;
}

export async function voiceTurn(formData: FormData): Promise<VoiceTurnResponse> {
  return request<VoiceTurnResponse>("/jarvis/voice/turn", {
    method: "POST",
    body: formData,
  });
}

export async function voiceTranscribe(formData: FormData): Promise<VoiceTranscribeResponse> {
  return request<VoiceTranscribeResponse>("/jarvis/voice/transcribe", {
    method: "POST",
    body: formData,
  });
}

export async function voiceTts(payload: VoiceTtsRequest): Promise<VoiceTtsResponse> {
  return request<VoiceTtsResponse>("/jarvis/voice/tts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
