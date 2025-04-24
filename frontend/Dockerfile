FROM python:3.9-slim

# Install FFmpeg and build dependencies for spaCy
RUN apt-get update && \
    apt-get install -y ffmpeg build-essential python3-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Increase pip timeout for large packages
ENV PIP_DEFAULT_TIMEOUT=300

COPY requirements.txt .
# Instalar numpy primeiro para evitar problemas de compatibilidade
RUN pip install --no-cache-dir numpy==1.24.3 && \
    pip install --no-cache-dir -r requirements.txt

# Baixar modelo do spaCy para português
RUN python -m spacy download pt_core_news_sm

# Configurar variáveis de ambiente para os serviços
ENV TRANSCRIPTION_SERVICE_URL=http://transcription:8002
ENV PREPROCESSING_SERVICE_URL=http://preprocessing:8001

COPY . .

EXPOSE 8000

CMD ["python", "app.py"]
