# Nginx reverse proxy for production
FROM nginx:alpine

# Copy config and make it simple (single COPY, no variables)
COPY docker/nginx/nginx.prod.conf /etc/nginx/conf.d/default.conf

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD wget --no-verbose --tries=1 --spider http://127.0.0.1/health || exit 1

EXPOSE 80

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
