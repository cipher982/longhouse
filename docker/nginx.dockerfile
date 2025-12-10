# Nginx reverse proxy for production
# Bakes config into image - no volume mounts needed

FROM nginx:alpine

# Copy nginx configuration into image
COPY docker/nginx/nginx.prod.conf /etc/nginx/conf.d/default.conf

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1/health || exit 1

EXPOSE 80

# Run nginx in foreground
CMD ["nginx", "-g", "daemon off;"]
