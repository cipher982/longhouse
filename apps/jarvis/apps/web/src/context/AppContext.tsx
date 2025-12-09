/**
 * Application Context for Jarvis PWA
 * Replaces the vanilla TypeScript StateManager with React Context
 */

import { createContext, useContext, useReducer, type ReactNode, type Dispatch } from 'react'
import type { AppState, AppAction } from './types'

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
  cachedAgents: [],

  // Media
  sharedMicStream: null,
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
    case 'SET_CACHED_AGENTS':
      return { ...state, cachedAgents: action.agents }
    case 'SET_MIC_STREAM':
      return { ...state, sharedMicStream: action.stream }
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
export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(appReducer, initialState)

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
