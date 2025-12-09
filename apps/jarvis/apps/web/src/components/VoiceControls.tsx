/**
 * VoiceControls component - Push-to-talk and hands-free mode
 */

export type VoiceMode = 'push-to-talk' | 'hands-free'
export type VoiceStatus = 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

interface VoiceControlsProps {
  mode: VoiceMode
  status: VoiceStatus
  onModeToggle: () => void
  onVoiceButtonPress: () => void
  onVoiceButtonRelease: () => void
}

const STATUS_TEXT: Record<VoiceStatus, string> = {
  ready: 'Ready',
  listening: 'Listening...',
  processing: 'Processing...',
  speaking: 'Speaking...',
  error: 'Error',
}

export function VoiceControls({
  mode,
  status,
  onModeToggle,
  onVoiceButtonPress,
  onVoiceButtonRelease,
}: VoiceControlsProps) {
  const isHandsFree = mode === 'hands-free'

  return (
    <div className="voice-controls">
      {/* Mode Toggle Switch */}
      <div className="mode-toggle-container">
        <span className="mode-label">Push-to-Talk</span>
        <button
          className={`mode-toggle ${isHandsFree ? 'active' : ''}`}
          type="button"
          role="switch"
          aria-checked={isHandsFree}
          aria-label="Toggle hands-free mode"
          onClick={onModeToggle}
        >
          <span className="mode-toggle-slider"></span>
        </button>
        <span className="mode-label">Hands-free</span>
      </div>

      {/* Main Voice Button */}
      <div className="voice-button-wrapper">
        <button
          className={`voice-button ${status}`}
          type="button"
          aria-label="Press to talk"
          onMouseDown={onVoiceButtonPress}
          onMouseUp={onVoiceButtonRelease}
          onMouseLeave={onVoiceButtonRelease}
          onTouchStart={onVoiceButtonPress}
          onTouchEnd={onVoiceButtonRelease}
        >
          <div className="voice-button-ring"></div>
          <svg className="voice-icon" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z" />
            <path d="M19 10v2a7 7 0 01-14 0v-2" />
            <line x1="12" y1="19" x2="12" y2="23" />
            <line x1="8" y1="23" x2="16" y2="23" />
          </svg>
        </button>
      </div>

      {/* Status Text */}
      <div className="voice-status">
        <span className="voice-status-text" aria-live="polite">
          {STATUS_TEXT[status]}
        </span>
      </div>
    </div>
  )
}
