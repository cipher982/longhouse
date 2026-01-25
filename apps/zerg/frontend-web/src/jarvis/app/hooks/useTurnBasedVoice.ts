/**
 * useTurnBasedVoice hook - Turn-based voice (record -> upload -> respond)
 */

import { useCallback, useEffect, useRef } from "react";
import { useAppDispatch, useAppState, type ChatMessage, type VoiceStatus } from "../context";
import { voiceTurn } from "../../../services/api";
import { uuid } from "../../lib/uuid";
import { logger } from "../../core";

const PREFERRED_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/mp4",
  "audio/mpeg",
  "audio/wav",
];

const DEFAULT_CONTENT_TYPE = "audio/webm";

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  return PREFERRED_MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
}

function base64ToBlob(base64: string, contentType: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new Blob([bytes], { type: contentType });
}

export interface UseTurnBasedVoiceOptions {
  onError?: (error: Error) => void;
}

export function useTurnBasedVoice(options: UseTurnBasedVoiceOptions = {}) {
  const dispatch = useAppDispatch();
  const { preferences } = useAppState();
  const statusRef = useRef<VoiceStatus>("idle");
  const recordingRef = useRef(false);
  const processingRef = useRef(false);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const setVoiceStatus = useCallback((status: VoiceStatus) => {
    statusRef.current = status;
    dispatch({ type: "SET_VOICE_STATUS", status });
  }, [dispatch]);

  const cleanupStream = useCallback(() => {
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
      dispatch({ type: "SET_MIC_STREAM", stream: null });
    }
  }, [dispatch]);

  const cleanupAudio = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = "";
      audioRef.current = null;
    }
  }, []);

  const pushErrorMessage = useCallback((message: string) => {
    const errorMessage: ChatMessage = {
      id: uuid(),
      role: "assistant",
      content: message,
      status: "error",
      timestamp: new Date(),
    };
    dispatch({ type: "ADD_MESSAGE", message: errorMessage });
  }, [dispatch]);

  const handleError = useCallback((message: string, error?: Error) => {
    logger.error(`[useTurnBasedVoice] ${message}`, error);
    setVoiceStatus("error");
    pushErrorMessage(`Voice error: ${message}`);
    options.onError?.(error ?? new Error(message));
  }, [options, pushErrorMessage, setVoiceStatus]);

  const playAudio = useCallback(async (audioBase64: string, contentType: string) => {
    if (!audioBase64) {
      setVoiceStatus("ready");
      return;
    }

    cleanupAudio();
    setVoiceStatus("speaking");

    try {
      const blob = base64ToBlob(audioBase64, contentType || "audio/mpeg");
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audioRef.current = audio;

      audio.onended = () => {
        URL.revokeObjectURL(url);
        cleanupAudio();
        setVoiceStatus("ready");
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        cleanupAudio();
        setVoiceStatus("ready");
      };

      await audio.play();
    } catch (error) {
      setVoiceStatus("ready");
      logger.warn("[useTurnBasedVoice] Audio playback failed", error);
    }
  }, [cleanupAudio, setVoiceStatus]);

  const sendVoiceTurn = useCallback(async (blob: Blob, contentType: string) => {
    processingRef.current = true;
    setVoiceStatus("processing");

    try {
      const formData = new FormData();
      const filename = `voice-${Date.now()}.webm`;
      formData.append("audio", blob, filename);
      formData.append("return_audio", "true");
      if (preferences.chat_model) {
        formData.append("model", preferences.chat_model);
      }

      const response = await voiceTurn(formData);
      if (response.status !== "success") {
        handleError(response.error || "Voice turn failed");
        return;
      }

      const transcript = response.transcript?.trim() || "";
      if (!transcript) {
        handleError("Empty transcript from voice turn");
        return;
      }

      const userMessage: ChatMessage = {
        id: uuid(),
        role: "user",
        content: transcript,
        timestamp: new Date(),
      };

      const assistantText = response.response_text || "";
      const assistantMessage: ChatMessage = {
        id: uuid(),
        role: "assistant",
        content: assistantText,
        status: "final",
        timestamp: new Date(),
        runId: response.run_id ?? undefined,
      };

      dispatch({ type: "ADD_MESSAGE", message: userMessage });
      dispatch({ type: "ADD_MESSAGE", message: assistantMessage });

      const tts = response.tts;
      if (tts?.audio_base64 && !tts.error) {
        await playAudio(tts.audio_base64, tts.content_type || "audio/mpeg");
      } else {
        setVoiceStatus("ready");
      }
    } catch (error) {
      handleError("Failed to send voice turn", error as Error);
    } finally {
      processingRef.current = false;
    }
  }, [dispatch, handleError, playAudio, preferences.chat_model, setVoiceStatus]);

  const startRecording = useCallback(async () => {
    if (processingRef.current || recordingRef.current) return;

    if (typeof MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      handleError("Voice recording not supported in this browser");
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      streamRef.current = stream;
      dispatch({ type: "SET_MIC_STREAM", stream });

      const mimeType = pickMimeType();
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      recorderRef.current = recorder;
      chunksRef.current = [];

      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      recorder.onstop = () => {
        const recordedChunks = chunksRef.current;
        chunksRef.current = [];
        recorderRef.current = null;
        cleanupStream();
        recordingRef.current = false;

        if (!recordedChunks.length) {
          handleError("No audio captured");
          return;
        }

        const contentType = recorder.mimeType || mimeType || DEFAULT_CONTENT_TYPE;
        const blob = new Blob(recordedChunks, { type: contentType });
        void sendVoiceTurn(blob, contentType);
      };

      recorder.start();
      recordingRef.current = true;
      setVoiceStatus("listening");
    } catch (error) {
      handleError("Microphone access failed", error as Error);
      cleanupStream();
    }
  }, [cleanupStream, dispatch, handleError, sendVoiceTurn, setVoiceStatus]);

  const stopRecording = useCallback(() => {
    if (!recordingRef.current) return;
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") {
      recordingRef.current = false;
      return;
    }
    setVoiceStatus("processing");
    recorder.stop();
  }, [setVoiceStatus]);

  const resetVoice = useCallback(() => {
    cleanupAudio();
    cleanupStream();
    processingRef.current = false;
    recordingRef.current = false;
    setVoiceStatus("ready");
  }, [cleanupAudio, cleanupStream, setVoiceStatus]);

  useEffect(() => {
    setVoiceStatus("ready");
    dispatch({ type: "SET_VOICE_MODE", mode: "push-to-talk" });
    return () => {
      cleanupAudio();
      cleanupStream();
    };
  }, [cleanupAudio, cleanupStream, dispatch, setVoiceStatus]);

  return {
    startRecording,
    stopRecording,
    resetVoice,
  };
}
