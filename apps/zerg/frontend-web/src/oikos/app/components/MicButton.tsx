/**
 * MicButton component - Hold-to-talk microphone control with visualizer rings
 */

import { useEffect, useRef } from 'react'

import { useAppState } from '../context'
import { RadialVisualizer } from '../../lib/radial-visualizer'

export type VoiceStatus = 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

interface MicButtonProps {
  status: VoiceStatus
  disabled?: boolean
  level?: number
  onConnect: () => void
  onPressStart: () => void
  onPressEnd: () => void
}

export function MicButton({
  status,
  disabled = false,
  level = 0,
  onConnect,
  onPressStart,
  onPressEnd,
}: MicButtonProps) {
  const { sharedMicStream } = useAppState()
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const visualizerRef = useRef<RadialVisualizer | null>(null)
  const renderStateRef = useRef(0)
  const renderColorRef = useRef('#475569')
  const isConnected = status !== 'idle' && status !== 'connecting' && status !== 'error'
  const isConnecting = status === 'connecting'
  const isBusy = status === 'processing' || status === 'speaking'
  const isDisabled = disabled || isConnecting || isBusy
  const normalizedLevel = Math.max(0, Math.min(1, level))

  useEffect(() => {
    if (!wrapperRef.current) return
    const viz = new RadialVisualizer(wrapperRef.current, { outerInset: 12, minRadius: 22 })
    visualizerRef.current = viz
    return () => {
      viz.destroy()
      visualizerRef.current = null
    }
  }, [])

  useEffect(() => {
    if (visualizerRef.current) {
      visualizerRef.current.provideStream(sharedMicStream)
    }
  }, [sharedMicStream])

  useEffect(() => {
    const micActive = status === 'listening'
    renderStateRef.current = micActive ? 1 : 0
    renderColorRef.current = micActive ? '#06b6d4' : '#475569'

    const viz = visualizerRef.current
    if (!viz) return

    if (micActive && sharedMicStream) {
      void viz.start()
    } else {
      viz.stop()
    }

    viz.render(normalizedLevel, renderColorRef.current, renderStateRef.current)
  }, [normalizedLevel, sharedMicStream, status])

  const handleClick = () => {
    if (status === 'idle' || status === 'error') {
      onConnect()
    }
  }

  const handlePressStart = () => {
    if (isConnected && !isDisabled) {
      onPressStart()
    }
  }

  const handlePressEnd = () => {
    if (isConnected && !isDisabled) {
      onPressEnd()
    }
  }

  const getAriaLabel = () => {
    if (status === 'idle') return 'Connect to voice'
    if (status === 'error') return 'Retry voice connection'
    if (status === 'connecting') return 'Connecting...'
    if (status === 'processing') return 'Processing...'
    if (status === 'speaking') return 'Speaking...'
    if (status === 'listening') return 'Release to send'
    return 'Hold to talk'
  }

  return (
    <div
      className={`voice-button-wrapper compact ${status}`}
      ref={wrapperRef}
    >
      <button
        type="button"
        className={`voice-button mic-button ${status} ${isDisabled ? 'disabled' : ''}`}
        aria-label={getAriaLabel()}
        disabled={isDisabled}
        onClick={handleClick}
        onMouseDown={handlePressStart}
        onMouseUp={handlePressEnd}
        onMouseLeave={handlePressEnd}
        onTouchStart={handlePressStart}
        onTouchEnd={handlePressEnd}
      >
        <svg
          className="voice-icon"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        >
          <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z" />
          <path d="M19 10v2a7 7 0 01-14 0v-2" />
          <line x1="12" y1="19" x2="12" y2="23" />
          <line x1="8" y1="23" x2="16" y2="23" />
        </svg>
      </button>
    </div>
  )
}
