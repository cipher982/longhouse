const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = process.env.FRONTEND_PORT ? parseInt(process.env.FRONTEND_PORT) : 8002;
const FRONTEND_DIR = path.join(__dirname, '..', 'frontend', 'www');

// MIME types with proper WASM support
const MIME_TYPES = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.wasm': 'application/wasm',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon'
};

function serveFile(filePath, res) {
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('File not found');
      return;
    }

    const ext = path.extname(filePath);
    const contentType = MIME_TYPES[ext] || 'application/octet-stream';
    
    // Set proper headers for WASM files
    if (ext === '.wasm') {
      res.setHeader('Content-Type', 'application/wasm');
      // Add CORS headers for WASM
      res.setHeader('Access-Control-Allow-Origin', '*');
    }
    
    res.writeHead(200, { 'Content-Type': contentType });
    res.end(data);
  });
}

const server = http.createServer((req, res) => {
  const parsedUrl = url.parse(req.url);
  let pathname = parsedUrl.pathname;
  
  // Default to index.html for root
  if (pathname === '/') {
    pathname = '/index.html';
  }
  
  // Security: prevent directory traversal
  const safePath = path.normalize(pathname).replace(/^(\.\.[\/\\])+/, '');
  const filePath = path.join(FRONTEND_DIR, safePath);
  
  // Only serve files within the frontend directory
  if (!filePath.startsWith(FRONTEND_DIR)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }
  
  // Check if file exists
  fs.stat(filePath, (err, stats) => {
    if (err || !stats.isFile()) {
      // Try index.html for SPA routes
      const indexPath = path.join(FRONTEND_DIR, 'index.html');
      serveFile(indexPath, res);
    } else {
      serveFile(filePath, res);
    }
  });
});

server.listen(PORT, () => {
  console.log(`WASM-aware server running at http://localhost:${PORT}`);
  console.log(`Serving files from: ${FRONTEND_DIR}`);
});
