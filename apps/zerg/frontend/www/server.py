import http.server
import socketserver
import sys
import os
from urllib.parse import unquote

class CacheControlHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Disable caching for HTML files to ensure CSP updates are loaded
        if self.path.endswith('.html') or self.path == '/':
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()

    def do_GET(self):
        # SPA fallback: if file doesn't exist and no extension, serve React index.html
        path = self.translate_path(self.path)

        # If path exists, serve it normally
        if os.path.exists(path):
            return super().do_GET()

        # If requesting a file with extension (e.g., .js, .css, .png), let it 404
        if '.' in os.path.basename(unquote(self.path)):
            return super().do_GET()

        # Otherwise, serve React index.html for SPA routing
        self.path = '/react/index.html'
        return super().do_GET()

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
with socketserver.TCPServer(("", PORT), CacheControlHandler) as httpd:
    print(f"Serving at http://localhost:{PORT}")
    httpd.serve_forever()
