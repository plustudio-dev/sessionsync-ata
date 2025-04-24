import os
import json
import requests
from flask import Flask, request, jsonify
import logging
from datetime import datetime
import subprocess
import uuid
from werkzeug.utils import secure_filename

app = Flask(__name__)
# Configurações
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/app/uploads')
app.config['DATA_FOLDER'] = os.environ.get('DATA_FOLDER', '/app/data')

# Service URLs from environment variables
TRANSCRIPTION_SERVICE_URL = os.environ.get('TRANSCRIPTION_SERVICE_URL', 'http://localhost:5002')

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Ensure directories exist
os.makedirs(app.config['DATA_FOLDER'], exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def update_session_status(session_id, status, **kwargs):
    """Update session status in metadata file with additional information."""
    metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
    
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        
        # Atualizar status se fornecido
        if status:
            metadata['status'] = status
        
        # Adicionar timestamp de atualização
        metadata['last_updated'] = datetime.now().isoformat()
        
        # Adicionar informações adicionais
        for key, value in kwargs.items():
            metadata[key] = value
        
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        return True
    
    return False

import subprocess
import uuid

@app.route('/preprocess', methods=['POST'])
def preprocess_audio():
    """Endpoint para pré-processamento de áudio.
    
    Recebe um arquivo de áudio ou um caminho para um arquivo já existente,
    segmenta em partes menores e envia para o serviço de transcrição.
    """
    # Verificar se estamos recebendo um arquivo ou um JSON com caminho
    if 'file' in request.files:
        # Recebendo um arquivo diretamente
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'Nenhum arquivo selecionado'}), 400
        
        # Gerar ID de sessão único
        session_id = str(uuid.uuid4())
        
        # Criar diretório para a sessão
        session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
        os.makedirs(session_dir, exist_ok=True)
        
        # Salvar o arquivo
        filename = secure_filename(file.filename)
        file_path = os.path.join(session_dir, filename)
        file.save(file_path)
        
        # Criar metadados básicos
        metadata = {
            'session_id': session_id,
            'original_filename': filename,
            'upload_time': datetime.now().isoformat(),
            'file_path': file_path,
            'status': 'uploaded'
        }
        
        # Salvar metadados
        metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
    else:
        # Recebendo um JSON com informações do arquivo
        data = request.json
        if not data or 'session_id' not in data or 'file_path' not in data:
            return jsonify({'error': 'Missing required parameters'}), 400
        
        session_id = data['session_id']
        file_path = data['file_path']
        
        if not os.path.exists(file_path):
            return jsonify({'error': f'File not found: {file_path}'}), 404
    
    # Atualizar status da sessão
    update_session_status(session_id, 'preprocessing')
    
    try:
        # Criar diretório para os segmentos processados
        session_dir = os.path.join(app.config['DATA_FOLDER'], session_id)
        os.makedirs(session_dir, exist_ok=True)
        
        # Segmentar o áudio usando FFmpeg
        logger.info(f"Iniciando segmentação do áudio para a sessão {session_id}")
        update_session_status(session_id, 'preprocessing', status_detail='Segmentando áudio')
        
        # Padrão para os arquivos de segmento (usando WAV para melhor compatibilidade com PCM)
        segment_pattern = os.path.join(session_dir, "segment_%03d.wav")
        
        # Comando FFmpeg para segmentar o áudio com pré-processamento para melhorar a qualidade da transcrição
        command = [
            "ffmpeg", "-y", "-i", file_path,
            # Filtros de áudio para melhorar a qualidade da transcrição
            "-af", "highpass=f=200,lowpass=f=3000,volume=1.5,dynaudnorm",
            # Configuração de segmentação
            "-f", "segment", "-segment_time", "900",  # 15 minutos
            # Configuração de áudio otimizada para Whisper
            "-c:a", "pcm_s16le",  # Formato PCM 16-bit (melhor para transcrição)
            "-ac", "1",  # Mono
            "-ar", "16000",  # 16kHz (ideal para Whisper)
            segment_pattern
        ]
        
        # Executar o comando
        subprocess.run(command, check=True)
        
        # Listar os segmentos gerados
        segments = []
        for i, filename in enumerate(sorted([f for f in os.listdir(session_dir) if f.startswith("segment_") and f.endswith(".wav")])):
            segment_path = os.path.join(session_dir, filename)
            
            # Obter duração do segmento
            try:
                result = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', segment_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                duration = float(result.stdout.strip())
            except Exception:
                duration = 900  # Valor padrão de 15 minutos
            
            # Calcular tempo de início do segmento
            start_time = i * 900  # 15 minutos por segmento
            
            segments.append({
                'index': i,
                'filename': filename,
                'path': segment_path,
                'start_time': start_time,
                'end_time': start_time + duration,
                'duration': duration
            })
        
        if not segments:
            update_session_status(session_id, 'error', error_message='No segments generated')
            return jsonify({'error': 'No segments generated'}), 500
        
        # Atualizar metadados da sessão
        update_session_status(
            session_id, 
            'preprocessed',
            preprocessing_completed=datetime.now().isoformat(),
            segments=segments
        )
        
        # Enviar para o serviço de transcrição automaticamente
        try:
            response = requests.post(
                f"{TRANSCRIPTION_SERVICE_URL}/transcribe",
                json={
                    'session_id': session_id,
                    'segments': segments
                }
            )
            
            if response.status_code != 200:
                logger.error(f"Error sending to transcription service: {response.text}")
                update_session_status(
                    session_id, 
                    'error', 
                    error_message=f'Failed to send to transcription service: {response.text}'
                )
                return jsonify({'error': 'Failed to send to transcription service'}), 500
                
            # Atualizar status para 'transcribing'
            update_session_status(session_id, 'transcribing')
        except requests.RequestException as e:
            logger.error(f"Error connecting to transcription service: {str(e)}")
            update_session_status(
                session_id, 
                'preprocessed',  # Manter como preprocessed para permitir retry
                error_message=f'Error connecting to transcription service: {str(e)}'
            )
            return jsonify({
                'status': 'preprocessed',
                'warning': f'Error connecting to transcription service: {str(e)}',
                'session_id': session_id,
                'segments_count': len(segments)
            })
        
        return jsonify({
            'status': 'success',
            'message': 'Audio preprocessing completed and sent to transcription service',
            'session_id': session_id,
            'segments_count': len(segments)
        })
        
    except Exception as e:
        logger.error(f"Error during preprocessing: {str(e)}")
        update_session_status(session_id, 'error', error_message=str(e))
        return jsonify({'error': f'Error during preprocessing: {str(e)}'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint para verificação de saúde do serviço."""
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8001, debug=True)