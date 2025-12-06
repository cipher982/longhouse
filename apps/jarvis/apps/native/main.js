const { app, BrowserWindow, globalShortcut, systemPreferences, Tray, Menu, shell, nativeImage } = require('electron');
const path = require('path');

// Configuration
const JARVIS_URL = process.env.JARVIS_URL || 'http://localhost:8080';
const HOTKEY = process.env.JARVIS_HOTKEY || 'Command+J';

let mainWindow;
let tray;
let isQuitting = false;

// Create the main window
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 720,
    height: 520,
    show: false,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: true,
    skipTaskbar: true,
    titleBarStyle: 'hiddenInset',
    vibrancy: 'sidebar', // macOS translucency
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      enableRemoteModule: false,
      webSecurity: true
    }
  });

  // Load the Jarvis PWA
  mainWindow.loadURL(JARVIS_URL);

  // Handle window events
  mainWindow.once('ready-to-show', () => {
    console.log('🎬 Jarvis window ready');
  });

  // Auto-hide when losing focus (Spotlight-style behavior)
  mainWindow.on('blur', () => {
    if (!mainWindow.webContents.isDevToolsOpened()) {
      mainWindow.hide();
    }
  });

  // Prevent window from being closed, just hide it
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  // Handle external links
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Debug logging
  mainWindow.webContents.on('console-message', (event, level, message) => {
    console.log(`[Renderer] ${message}`);
  });
}

// Create system tray
function createTray() {
  // Use a simple system icon that exists
  const iconPath = '/System/Library/CoreServices/Menu Extras/Volume.menu/Contents/Resources/Volume.icns';

  try {
    tray = new Tray(iconPath);
  } catch (error) {
    console.log('⚠️ Using default icon');
    // Fallback to a simple icon
    tray = new Tray(nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='));
  }
  tray.setToolTip('Jarvis Voice Agent');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show Jarvis',
      click: () => showWindow()
    },
    {
      label: 'Open in Browser',
      click: () => shell.openExternal(JARVIS_URL)
    },
    {
      type: 'separator'
    },
    {
      label: `Hotkey: ${HOTKEY}`,
      enabled: false
    },
    {
      type: 'separator'
    },
    {
      label: 'Quit Jarvis',
      click: () => quitApp()
    }
  ]);

  tray.setContextMenu(contextMenu);

  // Single click to toggle (like Spotlight)
  tray.on('click', () => {
    toggleWindow();
  });
}

// Window management functions
function showWindow() {
  if (!mainWindow.isVisible()) {
    // Center on screen
    const { screen } = require('electron');
    const primaryDisplay = screen.getPrimaryDisplay();
    const { width, height } = primaryDisplay.workAreaSize;

    const windowWidth = 720;
    const windowHeight = 520;
    const x = Math.floor((width - windowWidth) / 2);
    const y = Math.floor((height - windowHeight) / 3); // Slightly higher than center

    mainWindow.setPosition(x, y);
    mainWindow.show();
    mainWindow.focus();

    console.log('👁️ Jarvis window shown');
  }
}

function hideWindow() {
  if (mainWindow.isVisible()) {
    mainWindow.hide();
    console.log('🫥 Jarvis window hidden');
  }
}

function toggleWindow() {
  if (mainWindow.isVisible()) {
    hideWindow();
  } else {
    showWindow();
  }
}

function quitApp() {
  isQuitting = true;
  globalShortcut.unregisterAll();
  app.quit();
}

// App event handlers
app.whenReady().then(async () => {
  console.log('🚀 Jarvis native app starting...');

  // Request microphone permission upfront
  try {
    const micAccess = await systemPreferences.askForMediaAccess('microphone');
    console.log(`🎤 Microphone access: ${micAccess ? 'granted' : 'denied'}`);
  } catch (error) {
    console.error('❌ Microphone permission error:', error);
  }

  // Create window and tray
  createWindow();
  createTray();

  // Register global hotkey
  const success = globalShortcut.register(HOTKEY, () => {
    console.log(`⌨️ Global hotkey ${HOTKEY} pressed`);
    toggleWindow();
  });

  if (!success) {
    console.error(`❌ Failed to register global hotkey: ${HOTKEY}`);
  } else {
    console.log(`✅ Global hotkey registered: ${HOTKEY}`);
  }

  console.log(`✅ Jarvis ready - Press ${HOTKEY} to activate`);
});

// Handle all windows closed (keep app running in background)
app.on('window-all-closed', (event) => {
  event.preventDefault();
  // Keep running in background for global hotkey
});

// Handle app activation (clicking dock icon)
app.on('activate', () => {
  showWindow();
});

// Handle before quit
app.on('before-quit', () => {
  isQuitting = true;
});

// Cleanup on quit
app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

// Security: prevent new window creation
app.on('web-contents-created', (event, contents) => {
  contents.on('new-window', (event) => {
    event.preventDefault();
  });
});

// Handle app URL schemes (if needed later)
app.setAsDefaultProtocolClient('jarvis');

// Export for debugging
module.exports = { showWindow, hideWindow, toggleWindow };
