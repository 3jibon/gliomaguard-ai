FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies (CPU-only PyTorch for fast 1-minute cloud builds)
COPY requirements.txt .
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Copy all application assets and trained model checkpoint
COPY . .

# Hugging Face Spaces standard port is 7860
EXPOSE 7860
ENV PORT=7860

CMD ["python", "server.py"]
