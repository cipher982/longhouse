/**
 * JarvisChatPage - Entry point for the Jarvis chat UI within the Zerg SPA
 *
 * This wraps the Jarvis React app (formerly standalone) in its context provider
 * and loads the Jarvis-specific CSS styles.
 */

// Import Jarvis styles
import '../jarvis/styles/index.css';

// Import Jarvis app and context
import { AppProvider } from '../jarvis/app/context';
import App from '../jarvis/app/App';

export default function JarvisChatPage() {
  return (
    <AppProvider>
      <App />
    </AppProvider>
  );
}
