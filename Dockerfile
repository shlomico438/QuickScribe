FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    cmake \
    libomp-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements (without whisperx, torch, torchaudio)
COPY requirements.txt .

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install CPU PyTorch + torchaudio (torchvision not needed)
# RUN pip install --no-cache-dir torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu

# Install remaining Python dependencies
  RUN pip install --no-cache-dir -r requirements.txt

# Pin transformers and whisperx compatible versions
# RUN pip install --no-cache-dir "transformers<4.37" whisperx==3.1.1
  RUN pip install --no-cache-dir whisperx
# Force compatible NumPy version last (prevents upgrades from other packages)
# RUN pip install --no-cache-dir numpy==1.26.4

# Download NLTK punkt tokenizer
# RUN python -m nltk.downloader punkt

# Copy app
COPY app.py .

CMD ["python", "app.py"]
