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

export async function voiceTurn(formData: FormData): Promise<VoiceTurnResponse> {
  return request<VoiceTurnResponse>("/jarvis/voice/turn", {
    method: "POST",
    body: formData,
  });
}
