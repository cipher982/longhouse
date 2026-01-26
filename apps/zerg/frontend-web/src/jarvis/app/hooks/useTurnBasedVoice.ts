/**
 * useTurnBasedVoice hook - Turn-based voice (record -> upload -> respond)
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useAppDispatch, useAppState, type ChatMessage, type VoiceStatus } from "../context";
import { voiceTurn, ApiError } from "../../../services/api";
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
const MIN_AUDIO_BYTES = 2048;

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

type VoicePlaceholders = {
  userItemId?: string;
  assistantMessageId?: string;
};

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
  const [micLevel, setMicLevel] = useState(0);
  const micLevelRef = useRef(0);
  const micAudioCtxRef = useRef<AudioContext | null>(null);
  const micAnalyserRef = useRef<AnalyserNode | null>(null);
  const micAnalyserDataRef = useRef<Uint8Array<ArrayBuffer> | null>(null);
  const micSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const micRafRef = useRef<number | null>(null);

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
    if (micRafRef.current) {
      cancelAnimationFrame(micRafRef.current);
      micRafRef.current = null;
    }
    if (micSourceRef.current) {
      try {
        micSourceRef.current.disconnect();
      } catch {
        // Ignore cleanup errors.
      }
      micSourceRef.current = null;
    }
    if (micAudioCtxRef.current) {
      micAudioCtxRef.current.close().catch(() => undefined);
      micAudioCtxRef.current = null;
    }
    micAnalyserRef.current = null;
    micAnalyserDataRef.current = null;
    micLevelRef.current = 0;
    setMicLevel(0);
  }, [dispatch]);

  const startMicVisualizer = useCallback(async (stream: MediaStream) => {
    try {
      if (typeof AudioContext === "undefined" && !(window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext) {
        return;
      }
      if (typeof MediaStream !== "undefined" && !(stream instanceof MediaStream)) {
        return;
      }

      if (micRafRef.current) {
        cancelAnimationFrame(micRafRef.current);
        micRafRef.current = null;
      }

      if (micAudioCtxRef.current) {
        micAudioCtxRef.current.close().catch(() => undefined);
      }

      const AudioCtx = window.AudioContext || (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) return;

      const audioContext = new AudioCtx();
      micAudioCtxRef.current = audioContext;

      if (audioContext.state === "suspended") {
        await audioContext.resume().catch(() => undefined);
      }

      const source = audioContext.createMediaStreamSource(stream);
      micSourceRef.current = source;
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.8;
      source.connect(analyser);
      micAnalyserRef.current = analyser;

      const dataArray = new Uint8Array(new ArrayBuffer(analyser.fftSize));
      micAnalyserDataRef.current = dataArray;

      const update = () => {
        if (!micAnalyserRef.current || !micAnalyserDataRef.current) return;

        micAnalyserRef.current.getByteTimeDomainData(micAnalyserDataRef.current);
        let sumSquares = 0;
        for (let i = 0; i < micAnalyserDataRef.current.length; i += 1) {
          const centered = (micAnalyserDataRef.current[i] - 128) / 128;
          sumSquares += centered * centered;
        }
        const rms = Math.sqrt(sumSquares / micAnalyserDataRef.current.length);
        const rawLevel = Math.min(1, rms * 3.2);
        const smoothed = micLevelRef.current * 0.7 + rawLevel * 0.3;
        micLevelRef.current = smoothed;
        setMicLevel(smoothed);
        micRafRef.current = requestAnimationFrame(update);
      };

      update();
    } catch (error) {
      logger.warn("[useTurnBasedVoice] Mic visualizer unavailable", error);
    }
  }, []);

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

  const updatePlaceholders = useCallback((message: string, context?: VoicePlaceholders, assistantStatus: "final" | "error" = "error", userFallback?: string) => {
    if (context?.assistantMessageId) {
      dispatch({
        type: "UPDATE_MESSAGE_BY_MESSAGE_ID",
        messageId: context.assistantMessageId,
        updates: {
          content: message,
          status: assistantStatus,
          timestamp: new Date(),
        },
      });
    } else {
      pushErrorMessage(message);
    }

    if (context?.userItemId && userFallback) {
      dispatch({ type: "UPDATE_MESSAGE", itemId: context.userItemId, content: userFallback });
    }
  }, [dispatch, pushErrorMessage]);

  const handleError = useCallback((message: string, error?: Error, context?: VoicePlaceholders) => {
    logger.error(`[useTurnBasedVoice] ${message}`, error);
    setVoiceStatus("error");
    updatePlaceholders(`Voice error: ${message}`, context, "error", "Voice input failed");
    options.onError?.(error ?? new Error(message));
  }, [options, setVoiceStatus, updatePlaceholders]);

  const handleSoftError = useCallback((message: string, error?: Error, context?: VoicePlaceholders) => {
    logger.warn(`[useTurnBasedVoice] ${message}`, error);
    updatePlaceholders(message, context, "final", "No speech detected");
    setVoiceStatus("ready");
    if (error) {
      options.onError?.(error);
    }
  }, [options, setVoiceStatus, updatePlaceholders]);

  const createPlaceholders = useCallback((): VoicePlaceholders => {
    const userItemId = uuid();
    const assistantMessageId = uuid();

    const userMessage: ChatMessage = {
      id: uuid(),
      role: "user",
      content: "Transcribing...",
      timestamp: new Date(),
      itemId: userItemId,
      skipAnimation: true,
    };

    const assistantMessage: ChatMessage = {
      id: uuid(),
      role: "assistant",
      content: "",
      status: "typing",
      timestamp: new Date(),
      messageId: assistantMessageId,
    };

    dispatch({ type: "ADD_MESSAGE", message: userMessage });
    dispatch({ type: "ADD_MESSAGE", message: assistantMessage });

    return { userItemId, assistantMessageId };
  }, [dispatch]);

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
    const placeholders = createPlaceholders();

    try {
      const formData = new FormData();
      const filename = `voice-${Date.now()}.webm`;
      formData.append("audio", blob, filename);
      formData.append("return_audio", "true");
      if (preferences.chat_model) {
        formData.append("model", preferences.chat_model);
      }
      // Send messageId for backend correlation (unified text/voice architecture)
      if (placeholders.assistantMessageId) {
        formData.append("message_id", placeholders.assistantMessageId);
      }

      const response = await voiceTurn(formData);
      if (response.status !== "success") {
        handleError(response.error || "Voice turn failed", undefined, placeholders);
        return;
      }

      const transcript = response.transcript?.trim() || "";
      if (!transcript) {
        handleSoftError("Didn't catch that — try speaking a bit longer.", undefined, placeholders);
        return;
      }
      if (placeholders.userItemId) {
        dispatch({ type: "UPDATE_MESSAGE", itemId: placeholders.userItemId, content: transcript });
      }

      const assistantText = response.response_text || "";
      if (placeholders.assistantMessageId) {
        dispatch({
          type: "UPDATE_MESSAGE_BY_MESSAGE_ID",
          messageId: placeholders.assistantMessageId,
          updates: {
            content: assistantText,
            status: "final",
            timestamp: new Date(),
            runId: response.run_id ?? undefined,
          },
        });
      }

      const tts = response.tts;
      if (tts?.audio_base64 && !tts.error) {
        await playAudio(tts.audio_base64, tts.content_type || "audio/mpeg");
      } else {
        setVoiceStatus("ready");
      }
    } catch (error) {
      if (error instanceof ApiError) {
        const detail = typeof error.body === "object" && error.body && "detail" in error.body
          ? String((error.body as { detail?: string }).detail)
          : error.message;
        const friendly = detail === "Empty transcription result" || detail === "Audio too short"
          ? "Didn't catch that — try speaking a bit longer."
          : detail;
        if (detail === "Empty transcription result" || detail === "Audio too short") {
          handleSoftError(friendly, error, placeholders);
        } else {
          handleError(friendly, error, placeholders);
        }
      } else {
        handleError("Failed to send voice turn", error as Error, placeholders);
      }
    } finally {
      processingRef.current = false;
    }
  }, [createPlaceholders, dispatch, handleError, handleSoftError, playAudio, preferences.chat_model, setVoiceStatus]);

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
      await startMicVisualizer(stream);

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
        if (blob.size < MIN_AUDIO_BYTES) {
          handleSoftError("Didn't catch that — try speaking a bit longer.");
          return;
        }
        void sendVoiceTurn(blob, contentType);
      };

      recorder.start();
      recordingRef.current = true;
      setVoiceStatus("listening");
    } catch (error) {
      handleError("Microphone access failed", error as Error);
      cleanupStream();
    }
  }, [cleanupStream, dispatch, handleError, sendVoiceTurn, setVoiceStatus, startMicVisualizer]);

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
    micLevel,
  };
}
