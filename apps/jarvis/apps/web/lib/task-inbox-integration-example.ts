/**
 * Task Inbox Integration Example
 *
 * Shows how to integrate the Task Inbox component into Jarvis main.ts
 */

import { createTaskInbox } from './task-inbox';

// Example integration in main.ts:

async function initializeTaskInbox() {
  // Get container element (add to index.html)
  const container = document.getElementById('task-inbox-container');

  if (!container) {
    console.warn('Task inbox container not found in DOM');
    return;
  }

  // Get configuration from environment
  const zergApiURL = '/api';  // Same-origin API calls through nginx proxy
  // SaaS model: Task Inbox uses the logged-in user's JWT (localStorage.zerg_jwt).
  // If not logged in, the API will return 401 and surface via onError.

  try {
    // Create and initialize task inbox
    const inbox = await createTaskInbox(container, {
      apiURL: zergApiURL,
      onError: (error) => {
        console.error('Task Inbox error:', error);
        // Show error notification to user
      },
      onRunUpdate: (run) => {
        // Optional: Speak notification when run completes
        if (run.status === 'success' && run.summary) {
          // Integrate with voice output
          console.log('Run completed:', run.agent_name, run.summary);
        }
      },
    });

    console.log('Task Inbox initialized successfully');

    // Store globally for access from other components
    (window as any).__taskInbox = inbox;

  } catch (error) {
    console.error('Failed to initialize Task Inbox:', error);
  }
}

// Add to index.html:
/*
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8">
    <title>Jarvis</title>
    <link rel="stylesheet" href="styles.css">
    <link rel="stylesheet" href="styles/task-inbox.css">
  </head>
  <body>
    <div class="app-container">
      <!-- Existing Jarvis UI -->
      <div id="main-interface">
        <!-- Voice visualizer, conversation, etc. -->
      </div>

      <!-- Task Inbox Sidebar -->
      <aside id="task-inbox-container" class="sidebar">
        <!-- Task inbox renders here -->
      </aside>
    </div>
    <script type="module" src="/main.ts"></script>
  </body>
</html>
*/

// CSS for layout:
/*
.app-container {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

#main-interface {
  flex: 1;
  overflow: hidden;
}

.sidebar {
  width: 320px;
  border-left: 1px solid var(--border-color);
  overflow: hidden;
}

@media (max-width: 768px) {
  .app-container {
    flex-direction: column;
  }

  .sidebar {
    width: 100%;
    height: 40%;
    border-left: none;
    border-top: 1px solid var(--border-color);
  }
}
*/

export { initializeTaskInbox };
