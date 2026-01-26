/**
 * Application Context for Jarvis PWA
 * Replaces the vanilla TypeScript StateManager with React Context
 */

import { createContext, useContext, useReducer, type ReactNode, type Dispatch } from 'react'
import type { AppState, AppAction, ChatMessage } from './types'
import { uuid } from '../../lib/uuid'

/**
 * Initial application state
 */
const initialState: AppState = {
  // Core objects
  agent: null,
  session: null,
  sessionManager: null,

  // Conversation state
  messages: [],
  streamingContent: '',
  userTranscriptPreview: '',
  currentConversationId: null,
  conversations: [],

  // Voice state
  voiceMode: 'push-to-talk',
  voiceStatus: 'idle',

  // UI state
  sidebarOpen: false,
  isConnected: false,

  // Jarvis-Zerg integration
  jarvisClient: null,
  cachedFiches: [],

  // Media
  sharedMicStream: null,

  // Chat preferences
  availableModels: [],
  preferences: {
    chat_model: 'gpt-5.2',
    reasoning_effort: 'none',
  },
}

/**
 * Reducer for state updates
 */
function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'SET_SESSION':
      return { ...state, session: action.session, isConnected: action.session !== null }
    case 'SET_AGENT':
      return { ...state, agent: action.agent }
    case 'SET_SESSION_MANAGER':
      return { ...state, sessionManager: action.sessionManager }
    case 'SET_MESSAGES':
      return { ...state, messages: action.messages }
    case 'ADD_MESSAGE':
      return { ...state, messages: [...state.messages, action.message] }
    case 'UPDATE_MESSAGE': {
      const idx = state.messages.findIndex(m => m.itemId === action.itemId)
      if (idx === -1) return state
      const updated = [...state.messages]
      updated[idx] = { ...updated[idx], content: action.content }
      return { ...state, messages: updated }
    }
    case 'UPDATE_MESSAGE_BY_MESSAGE_ID': {
      // Message IDs are stable across a single concierge course.
      // For normal courses: Client-generated upfront (placeholder already has messageId).
      // For continuation courses: Backend-generated, create new message on first content update.
      const idx = state.messages.findIndex(
        (m) => m.role === 'assistant' && m.messageId === action.messageId
      )
      if (idx === -1) {
        // Message doesn't exist yet - this is a continuation course
        // Guard: Don't create empty bubble if content is empty/undefined
        // This prevents empty assistant bubbles from appearing before tokens arrive
        if (!action.updates.content) {
          return state
        }
        // Create a NEW assistant message with the given messageId
        const newMessage: ChatMessage = {
          id: uuid(),
          role: 'assistant',
          content: action.updates.content,
          timestamp: action.updates.timestamp || new Date(),
          messageId: action.messageId,
          status: action.updates.status,
          courseId: action.updates.courseId,
          usage: action.updates.usage,
        }
        return { ...state, messages: [...state.messages, newMessage] }
      }
      const updated = [...state.messages]
      updated[idx] = { ...updated[idx], ...action.updates }
      return { ...state, messages: updated }
    }
    case 'SET_STREAMING_CONTENT':
      return { ...state, streamingContent: action.content }
    case 'SET_USER_TRANSCRIPT_PREVIEW':
      return { ...state, userTranscriptPreview: action.text }
    case 'SET_CONVERSATION_ID':
      return { ...state, currentConversationId: action.id }
    case 'SET_CONVERSATIONS':
      return { ...state, conversations: action.conversations }
    case 'SET_VOICE_MODE':
      return { ...state, voiceMode: action.mode }
    case 'SET_VOICE_STATUS':
      return { ...state, voiceStatus: action.status }
    case 'SET_SIDEBAR_OPEN':
      return { ...state, sidebarOpen: action.open }
    case 'SET_CONNECTED':
      return { ...state, isConnected: action.connected }
    case 'SET_JARVIS_CLIENT':
      return { ...state, jarvisClient: action.client }
    case 'SET_CACHED_FICHES':
      return { ...state, cachedFiches: action.fiches }
    case 'SET_MIC_STREAM':
      return { ...state, sharedMicStream: action.stream }
    case 'SET_AVAILABLE_MODELS':
      return { ...state, availableModels: action.models }
    case 'SET_PREFERENCES':
      return { ...state, preferences: action.preferences }
    case 'UPDATE_PREFERENCE':
      return {
        ...state,
        preferences: {
          ...state.preferences,
          [action.key]: action.value,
        },
      }
    case 'RESET':
      return initialState
    default:
      return state
  }
}

/**
 * Context types
 */
interface AppContextValue {
  state: AppState
  dispatch: Dispatch<AppAction>
}

/**
 * Create context
 */
const AppContext = createContext<AppContextValue | null>(null)

/**
 * Provider component
 */
interface AppProviderProps {
  children: ReactNode;
  initialMessages?: AppState['messages'];
}

export function AppProvider({ children, initialMessages }: AppProviderProps) {
  const initState = initialMessages
    ? { ...initialState, messages: initialMessages }
    : initialState;
  const [state, dispatch] = useReducer(appReducer, initState)

  return <AppContext.Provider value={{ state, dispatch }}>{children}</AppContext.Provider>
}

/**
 * Hook to access app context
 */
export function useAppContext(): AppContextValue {
  const context = useContext(AppContext)
  if (!context) {
    throw new Error('useAppContext must be used within an AppProvider')
  }
  return context
}

/**
 * Hook to access just the state (convenience)
 */
export function useAppState(): AppState {
  return useAppContext().state
}

/**
 * Hook to access just the dispatch (convenience)
 */
export function useAppDispatch(): Dispatch<AppAction> {
  return useAppContext().dispatch
}
