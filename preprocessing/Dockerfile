FROM python:3.10-slim

# Install FFmpeg (única dependência necessária para o serviço simplificado)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Increase pip timeout for large packages
ENV PIP_DEFAULT_TIMEOUT=300

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set environment variables for better performance
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1

# Configure FFmpeg for better performance
ENV FFREPORT=level=32

COPY . .

EXPOSE 8001

CMD ["python", "app.py"]
