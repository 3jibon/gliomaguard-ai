# GliomaGuard Cloud Research Viewer v2.0.1 (Header overlap fixed)
FROM python:3.10-slim

WORKDIR /app

# Copy all application assets, precomputed audited research packages, and cloud entry point
COPY . .

# Render exposes PORT environment variable (default 10000)
EXPOSE 10000
ENV PORT=10000

CMD ["python", "cloud_server.py"]
