# Session Sync

Um sistema modular baseado em microserviços que processa áudios de sessões legislativas e gera atas estruturadas.

## Componentes

1. **Frontend** (Porta 5000)
   - Interface web para upload de áudios e visualização de resultados
   - Formulários para metadados da sessão e monitoramento de progresso

2. **Serviço de Pré-processamento** (Porta 5001)
   - Divide áudios longos em segmentos menores
   - Normalização de formato (conversão para MP3)
   - Detecção de silêncio e ruído

3. **Serviço de Transcrição** (Porta 5002)
   - Transcrição com timestamps usando OpenAI Whisper
   - Processamento paralelo de segmentos
   - Sistema de retry para segmentos problemáticos

## Requisitos

- Docker e Docker Compose
- Python 3.9+

## Execução

```bash
docker-compose up -d
```

Acesse o frontend em: http://localhost:5000
