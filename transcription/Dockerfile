FROM python:3.10-slim

# Install FFmpeg and other dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg libsndfile1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Increase pip timeout for large packages
ENV PIP_DEFAULT_TIMEOUT=300

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Whisper medium model during build
RUN python -c "import whisper; whisper.load_model('medium')"

# Set environment variables for better performance
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1

COPY . .

EXPOSE 8002

CMD ["python", "app.py"]
