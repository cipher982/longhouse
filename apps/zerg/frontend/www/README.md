## Architecture

The application follows a clean separation of concerns:

1. **HTML**: Provides the base structure with essential containers:
   - Header with title
   - App container (for Rust-generated UI)
   - Status bar

2. **CSS**: Contains all styling in a separate stylesheet (styles.css)
   - Responsive design for various screen sizes
   - Z-index management for proper layering
   - Full-viewport canvas

3. **Rust/WASM**: Handles all application logic:
   - Dynamically creates UI elements
   - Manages the canvas and drawing operations
   - Handles user interaction
   - Communicates with the backend

This architecture ensures clean separation between structure, presentation, and behavior, making the code more maintainable and easier to extend. 