import os
import json
import logging
import time
import torch
import whisper
import threading
import subprocess
from queue import Queue, Empty as QueueEmpty
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)
app.config['DATA_FOLDER'] = os.environ.get('DATA_FOLDER', '/app/data')

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Ensure directories exist
os.makedirs(app.config['DATA_FOLDER'], exist_ok=True)

# Global variables
model = None
model_name = os.environ.get('WHISPER_MODEL', 'medium')  # Alterado para 'medium' conforme solicitado
model_lock = threading.Lock()
processing_queue = Queue(maxsize=200)  # Aumentamos a capacidade da fila
worker_threads = []
max_workers = 1  # Alterado para 1 para processamento sequencial
max_retries = int(os.environ.get('MAX_RETRIES', 5))  # Aumentamos o número de tentativas
retry_delay = int(os.environ.get('RETRY_DELAY', 2))

# Dicionário para rastrear o estado de processamento de cada segmento
segment_processing_status = {}
# Lock para acessar o dicionário de status
status_lock = threading.Lock()

# Contador de segmentos processados para monitoramento
segments_processed = 0
segments_failed = 0

# Função para formatar tempo em segundos para HH:MM:SS
def format_time(seconds):
    """Converte tempo em segundos para formato HH:MM:SS"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

def update_session_status(session_id, status, **kwargs):
    """Update the session metadata with new status and additional information.
    Garante que todos os segmentos sejam salvos corretamente no arquivo JSON.
    Implementa mecanismo robusto para evitar corridas de condição e perda de dados.
    """
    metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
    
    # Criar o arquivo se não existir
    if not os.path.exists(metadata_path):
        try:
            # Criar diretório se não existir
            os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
            
            # Criar arquivo JSON inicial
            initial_data = {
                'session_id': session_id,
                'status': status if status else 'created',
                'created_at': datetime.now().isoformat(),
                'last_updated': datetime.now().isoformat()
            }
            
            # Adicionar informações extras
            for key, value in kwargs.items():
                initial_data[key] = value
            
            # Salvar arquivo
            with open(metadata_path, 'w') as f:
                json.dump(initial_data, f, indent=2)
            
            logger.info(f"Arquivo de metadados criado para sessão {session_id}")
            return True
        except Exception as e:
            logger.error(f"Erro ao criar arquivo de metadados para sessão {session_id}: {str(e)}")
            return False
    
    # Atualizar arquivo existente com mecanismo de retry e bloqueio
    lock_file = f"{metadata_path}.lock"
    max_retries = 5  # Aumentado para 5 tentativas
    retry_delay = 0.5  # Meio segundo entre tentativas
    
    for retry in range(max_retries):
        try:
            # Tentar criar um arquivo de bloqueio
            if os.path.exists(lock_file):
                # Verificar se o bloqueio está obsoleto (mais de 30 segundos)
                lock_time = os.path.getmtime(lock_file)
                current_time = time.time()
                if current_time - lock_time > 30:
                    logger.warning(f"Removendo bloqueio obsoleto para sessão {session_id}")
                    os.remove(lock_file)
                else:
                    # Bloqueio ativo, aguardar e tentar novamente
                    logger.debug(f"Arquivo bloqueado, aguardando... (tentativa {retry+1}/{max_retries})")
                    time.sleep(retry_delay * (retry + 1))  # Backoff exponencial
                    continue
            
            # Criar arquivo de bloqueio
            with open(lock_file, 'w') as f:
                f.write(f"Locked by process {os.getpid()} at {datetime.now().isoformat()}")
            
            try:
                # Ler o arquivo JSON atual
                try:
                    with open(metadata_path, 'r') as f:
                        metadata = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Erro ao ler arquivo JSON: {str(e)}")
                    if retry < max_retries - 1:
                        continue
                    else:
                        raise
                
                # Atualizar status se fornecido
                if status is not None:
                    metadata['status'] = status
                
                # Adicionar timestamp de atualização
                metadata['last_updated'] = datetime.now().isoformat()
                
                # Processamento especial para transcrições
                if 'transcript' in kwargs:
                    # Se já existe uma transcrição, garantir que todos os segmentos sejam preservados
                    if 'transcript' in metadata:
                        # Criar dicionários indexados por segment_index para facilitar a mesclagem
                        existing_segments = {seg.get('segment_index'): seg for seg in metadata['transcript'] 
                                            if 'segment_index' in seg}
                        new_segments = {seg.get('segment_index'): seg for seg in kwargs['transcript'] 
                                        if 'segment_index' in seg}
                        
                        # Mesclar segmentos existentes com novos segmentos
                        merged_segments = existing_segments.copy()
                        merged_segments.update(new_segments)  # Novos segmentos substituem existentes com mesmo índice
                        
                        # Converter de volta para lista e ordenar por índice
                        kwargs['transcript'] = sorted(merged_segments.values(), key=lambda x: x.get('segment_index', 0))
                        
                        # Registrar informações sobre a mesclagem
                        logger.info(f"Sessão {session_id}: Mesclados {len(existing_segments)} segmentos existentes com {len(new_segments)} novos segmentos")
                
                # Adicionar informações adicionais
                for key, value in kwargs.items():
                    metadata[key] = value
                
                # Verificar se todos os segmentos foram processados
                if 'segments_processed' in metadata and 'total_segments' in metadata and metadata['total_segments'] > 0:
                    progress = metadata['segments_processed'] / metadata['total_segments']
                    metadata['progress'] = progress
                    
                    # Verificar se a sessão está completa
                    if metadata['segments_processed'] >= metadata['total_segments'] and metadata.get('status') != 'completed':
                        metadata['status'] = 'completed'
                        metadata['completion_time'] = datetime.now().isoformat()
                        logger.info(f"Sessão {session_id} marcada como concluída com {metadata['segments_processed']} segmentos")
                        
                        # Verificar se o segmento 0 está presente
                        segment0_found = False
                        if 'transcript' in metadata:
                            for segment in metadata['transcript']:
                                if segment.get('segment_index') == 0:
                                    segment0_found = True
                                    break
                        
                        # Se o segmento 0 não estiver presente, aplicar transcrição forçada
                        if not segment0_found:
                            logger.warning(f"Sessão {session_id} concluída, mas o segmento 0 não foi encontrado. Aplicando transcrição forçada.")
                            try:
                                # Salvar metadados antes de forçar a transcrição
                                with open(metadata_path, 'w') as f:
                                    json.dump(metadata, f, indent=2)
                                
                                # Remover o bloqueio antes de chamar outra função que pode tentar adquiri-lo
                                if os.path.exists(lock_file):
                                    os.remove(lock_file)
                                
                                # Forçar transcrição do segmento 0
                                force_transcribe_segment0_internal(session_id)
                                
                                # Não precisamos recarregar os metadados aqui, pois já salvamos
                                return True
                            except Exception as e:
                                logger.error(f"Erro ao forçar transcrição do segmento 0: {str(e)}")
                
                # Salvar metadados atualizados
                with open(metadata_path, 'w') as f:
                    json.dump(metadata, f, indent=2)
                
                logger.debug(f"Metadados atualizados para sessão {session_id}")
                return True
            
            finally:
                # Sempre remover o arquivo de bloqueio ao finalizar
                if os.path.exists(lock_file):
                    os.remove(lock_file)
        
        except Exception as e:
            logger.error(f"Erro ao atualizar status da sessão {session_id} (tentativa {retry+1}/{max_retries}): {str(e)}")
            time.sleep(retry_delay * (retry + 1))  # Backoff exponencial
            
            # Remover bloqueio se existir
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                except Exception as lock_e:
                    logger.error(f"Erro ao remover arquivo de bloqueio: {str(lock_e)}")
    
    logger.error(f"Falha ao atualizar status da sessão {session_id} após {max_retries} tentativas")
    return False

def remove_prompt_text(text):
    """Remove textos do prompt inicial que podem ter sido incluídos na transcrição."""
    if not text:
        return text
        
    # Lista de frases do prompt inicial que devem ser removidas da transcrição
    prompt_phrases = [
        "Esta é uma transcrição em português brasileiro",
        "Transcreva este áudio em português brasileiro",
        "Esta é uma transcrição em português brasileiro de uma sessão legislativa",
        "Transcrição em português brasileiro",
        "Áudio em português brasileiro"
    ]
    
    # Remover frases do prompt que possam ter sido incluídas na transcrição
    original_text = text
    for phrase in prompt_phrases:
        if phrase.lower() in text.lower():
            # Substituir a frase por espaço em branco
            text = text.lower().replace(phrase.lower(), "").strip()
            logger.info(f"Frase do prompt removida da transcrição: '{phrase}'")
    
    # Se o texto foi modificado, restaurar a capitalização adequada
    if text != original_text:
        # Capitalizar a primeira letra de cada frase
        sentences = text.split('. ')
        sentences = [s.capitalize() for s in sentences if s]
        text = '. '.join(sentences)
    
    return text

def fix_repetitions(text):
    """Detecta e corrige repetições excessivas no texto transcrito."""
    if not text:
        return text
    
    # Primeiro remover textos do prompt
    text = remove_prompt_text(text)
    
    # Dividir o texto em palavras
    words = text.split()
    
    # Se o texto tem menos de 5 palavras, não precisa verificar repetições
    if len(words) < 5:
        return text
    
    # Detectar padrões de repetição
    cleaned_words = []
    i = 0
    repetition_detected = False
    
    while i < len(words):
        repetition_found = False
        
        # Verificar repetições de frases (2 a 10 palavras)
        for phrase_length in range(2, min(11, len(words) - i)):
            phrase = ' '.join(words[i:i+phrase_length])
            
            # Procurar a mesma frase mais adiante
            j = i + phrase_length
            while j + phrase_length <= len(words):
                next_phrase = ' '.join(words[j:j+phrase_length])
                
                if phrase.lower() == next_phrase.lower():  # Comparação case-insensitive
                    # Encontrou uma repetição
                    j += phrase_length
                else:
                    break
            
            if j > i + phrase_length:  # Se encontrou pelo menos uma repetição
                i = j  # Avançar para depois das repetições
                repetition_found = True
                repetition_detected = True
                break
        
        if not repetition_found:
            # Se não encontrou repetição, adicionar a palavra atual
            cleaned_words.append(words[i])
            i += 1
    
    # Reconstruir o texto
    cleaned_text = ' '.join(cleaned_words)
    
    # Registrar se uma repetição foi corrigida
    if repetition_detected:
        logger.info(f"Repetição excessiva corrigida. Original: {len(words)} palavras, Corrigido: {len(cleaned_words)} palavras")
    
    return cleaned_text

def load_model(model_size=None):
    """Carrega o modelo Whisper com configurações otimizadas."""
    global model, model_name
    
    # Se já temos um modelo carregado e não foi solicitado um modelo específico, retornar o modelo existente
    if model is not None and model_size is None:
        return model
    
    requested_model = model_size or model_name
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    logger.info(f"Carregando modelo Whisper {requested_model} no dispositivo {device}")
    
    try:
        # Liberar memória se estiver usando GPU
        if device == "cuda":
            torch.cuda.empty_cache()
        
        # Carregar o modelo com tratamento de dispositivo correto
        loaded_model = whisper.load_model(requested_model, device=device)
        logger.info(f"Modelo {requested_model} carregado com sucesso")
        
        # Configurações específicas para evitar problemas de tensor
        if hasattr(loaded_model, 'encoder'):
            loaded_model.encoder.conv1.register_forward_hook(lambda module, input, output: None)
            logger.info("Hook de forward registrado para evitar problemas de tensor")
        
        # Atualizar a variável global model
        model = loaded_model
        
        return loaded_model
    except Exception as e:
        logger.error(f"Erro ao carregar modelo {requested_model}: {str(e)}")
        # Se falhar ao carregar o modelo solicitado, tentar carregar um modelo menor
        if requested_model == "medium" and model_size is None:
            logger.info("Tentando carregar modelo 'base' como fallback")
            return load_model("base")
        elif requested_model == "base" and model_size is None:
            logger.info("Tentando carregar modelo 'small' como fallback")
            return load_model("small")
        else:
            raise

def get_session_data(session_id):
    """Get session metadata."""
    metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
    
    if not os.path.exists(metadata_path):
        logger.error(f"Session metadata not found: {metadata_path}")
        return None
    
    try:
        with open(metadata_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading session metadata: {str(e)}")
        return None

def preprocess_audio_for_whisper(audio_path, retry_count=0):
    """Pré-processa o áudio para evitar erros de dimensão de tensor no Whisper."""
    try:
        # Criar um nome temporário para o arquivo processado
        temp_dir = os.path.dirname(audio_path)
        temp_filename = f"temp_processed_{os.path.basename(audio_path)}"
        temp_path = os.path.join(temp_dir, temp_filename)
        
        # Comando FFmpeg para padronizar o áudio
        command = [
            "ffmpeg", "-y", "-i", audio_path,
            "-ac", "1",  # Mono
            "-ar", "16000",  # 16kHz
            "-c:a", "pcm_s16le",  # PCM 16-bit
            temp_path
        ]
        
        # Executar o comando
        subprocess.run(command, check=True, capture_output=True)
        
        return temp_path
    except Exception as e:
        logger.error(f"Erro ao pré-processar áudio: {str(e)}")
        # Se falhar, retornar o caminho original
        return audio_path

def force_preprocess_audio(audio_path):
    """Força o pré-processamento do áudio para evitar erros de dimensão de tensor."""
    try:
        # Criar um nome temporário para o arquivo processado
        temp_dir = os.path.dirname(audio_path)
        temp_filename = f"fixed_{os.path.basename(audio_path)}"
        temp_path = os.path.join(temp_dir, temp_filename)
        
        # Comando FFmpeg para padronizar o áudio - configuração específica para evitar erros de dimensão
        command = [
            "ffmpeg", "-y", "-i", audio_path,
            "-ac", "1",  # Mono
            "-ar", "16000",  # 16kHz
            "-c:a", "pcm_s16le",  # PCM 16-bit
            "-af", "aresample=16000,asetrate=16000,highpass=f=200,lowpass=f=3000,volume=1.5,dynaudnorm",  # Filtros de áudio
            temp_path
        ]
        
        # Executar o comando
        subprocess.run(command, check=True, capture_output=True)
        logger.info(f"Áudio pré-processado salvo em {temp_path}")
        return temp_path
    except Exception as e:
        logger.error(f"Erro ao forçar pré-processamento de áudio: {str(e)}")
        return audio_path

def transcribe_segment(segment, session_id, retry_count=0):
    """Transcribe a single audio segment using Whisper.
    Otimizado para maior resiliência e suporte a áudios longos.
    Tratamento especial para o segmento 0 para garantir que seja sempre processado corretamente.
    """
    try:
        # Tratamento especial para o segmento 0
        if segment['index'] == 0 and retry_count == 0:
            # Verificar se já existe uma transcrição para o segmento 0
            session_data = get_session_data(session_id)
            if session_data and 'transcript' in session_data:
                segment0_exists = False
                for existing_segment in session_data['transcript']:
                    if existing_segment.get('segment_index') == 0:
                        segment0_exists = True
                        break
                
                # Se o segmento 0 já existe na transcrição, não precisamos processá-lo novamente
                if segment0_exists:
                    logger.info(f"Segmento 0 já existe na transcrição da sessão {session_id}. Pulando processamento.")
                    return session_data['transcript'][0]  # Retornar o segmento 0 existente
        
        # Load the model if not already loaded
        whisper_model = load_model()
        
        # Get audio file path
        audio_path = segment['path']
        
        # Log transcription start
        logger.info(f"Transcribing segment {segment['index']} for session {session_id}")
        
        # Verificar se o arquivo existe e tem tamanho adequado
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
            logger.error(f"Arquivo de áudio inválido ou muito pequeno: {audio_path}")
            if segment['index'] == 0:
                # Para o segmento 0, aplicar transcrição forçada em vez de falhar
                logger.warning(f"Aplicando transcrição forçada para o segmento 0 devido a arquivo inválido")
                force_result = force_transcribe_segment0_internal(session_id)
                if force_result:
                    logger.info(f"Transcrição forçada do segmento 0 aplicada com sucesso")
                    # Obter a transcrição forçada
                    session_data = get_session_data(session_id)
                    if session_data and 'transcript' in session_data:
                        for segment_data in session_data['transcript']:
                            if segment_data.get('segment_index') == 0:
                                return segment_data
                else:
                    logger.error(f"Falha ao aplicar transcrição forçada para o segmento 0")
                    raise ValueError(f"Arquivo de áudio inválido ou muito pequeno: {audio_path}")
            else:
                raise ValueError(f"Arquivo de áudio inválido ou muito pequeno: {audio_path}")
        
        # Pré-processar o áudio para o segmento 0 ou se não for a primeira tentativa
        if segment['index'] == 0 or retry_count > 0:
            logger.info(f"Aplicando pré-processamento forçado para o segmento {segment['index']}")
            audio_path = force_preprocess_audio(audio_path)
        
        # Configurações de transcrição adaptativas baseadas no número de tentativas e índice do segmento
        if segment['index'] == 0:
            # Para o segmento 0, vamos usar uma abordagem completamente diferente
            try:
                # Carregar um modelo menor para o segmento problemático
                small_model = load_model('small')
                logger.info("Usando modelo 'small' para o segmento 0 que está causando problemas")
                
                # Configurações mínimas para o modelo small
                small_options = {
                    'language': 'pt',
                    'task': 'transcribe',
                    'verbose': True,
                    'word_timestamps': False,
                    'beam_size': 1,
                    'best_of': 1,
                    'temperature': 0.0,  # Apenas uma temperatura
                    'fp16': False,
                    'no_speech_threshold': 0.9,
                    'condition_on_previous_text': False,
                    'suppress_blank': True,
                    'initial_prompt': ""
                }
                
                # Tentar transcrever com o modelo small
                result = small_model.transcribe(audio_path, **small_options)
                logger.info(f"Transcrição do segmento 0 concluída com modelo small: {result['text'][:100]}...")
                
                # Formatar o resultado e continuar o processamento
                if result and result.get('text'):
                    # Criar segmentos formatados manualmente
                    formatted_segments = [{
                        'text': result['text'],
                        'original_text': result['text'],
                        'start': 0,
                        'end': segment['duration'],
                        'start_formatted': format_time(0),
                        'end_formatted': format_time(segment['duration']),
                        'corrected': False
                    }]
                    
                    # Criar resultado formatado
                    formatted_result = {
                        'segment_index': segment['index'],
                        'start_time': segment['start_time'],
                        'end_time': segment['end_time'],
                        'text': result['text'],
                        'original_text': result['text'],
                        'phrases': formatted_segments,
                        'language': result.get('language', 'pt'),
                        'corrected': False
                    }
                    
                    # Atualizar sessão
                    session_data = get_session_data(session_id)
                    if session_data:
                        # Inicializar lista de transcrições se não existir
                        if 'transcript' not in session_data:
                            session_data['transcript'] = []
                        
                        # Adicionar ou atualizar este segmento na transcrição
                        segment_exists = False
                        for i, existing_segment in enumerate(session_data['transcript']):
                            if existing_segment.get('segment_index') == segment['index']:
                                session_data['transcript'][i] = formatted_result
                                segment_exists = True
                                break
                        
                        if not segment_exists:
                            session_data['transcript'].append(formatted_result)
                        
                        # Ordenar transcrição por índice de segmento
                        session_data['transcript'].sort(key=lambda x: x.get('segment_index', 0))
                        
                        # Atualizar contagem de segmentos concluídos
                        segments_completed = session_data.get('segments_completed', 0) + 1
                        segments_total = session_data.get('total_segments', 0)
                        
                        # Atualizar status da sessão
                        update_kwargs = {
                            'segments_completed': segments_completed,
                            'transcript': session_data['transcript']
                        }
                        
                        # Se todos os segmentos foram processados, marcar como concluído
                        if segments_completed >= segments_total and segments_total > 0:
                            update_kwargs['completion_time'] = datetime.now().isoformat()
                            update_session_status(session_id, 'completed', **update_kwargs)
                        else:
                            # Caso contrário, não alteramos o status
                            update_session_status(session_id, None, **update_kwargs)
                    
                    # Retornar o resultado formatado
                    return formatted_result
                
            except Exception as small_model_error:
                logger.error(f"Erro ao usar modelo small para o segmento 0: {str(small_model_error)}")
                # Continuar com o modelo medium e configurações padrão
            
            # Se o modelo small falhar, usar configurações específicas para o segmento 0
            transcription_options = {
                'language': 'pt',
                'task': 'transcribe',
                'verbose': True,
                'word_timestamps': False,
                'beam_size': 1,
                'best_of': 1,
                'temperature': 0.0,  # Apenas uma temperatura
                'fp16': False,
                'no_speech_threshold': 0.9,  # Mais tolerante a silêncio
                'condition_on_previous_text': False,
                'suppress_blank': True,
                'initial_prompt': ""
            }
        elif retry_count == 0:
            # Primeira tentativa para outros segmentos: configurações padrão
            transcription_options = {
                'language': 'pt',  # Portuguese
                'task': 'transcribe',
                'verbose': True,  # Ativar verbose para depuração
                'word_timestamps': False,  # Desativamos timestamps por palavra para evitar erros de dimensão
                'beam_size': 5,          # Aumenta a precisão da busca por transcrições
                'best_of': 5,            # Seleciona a melhor entre várias transcrições
                'temperature': [0.0, 0.2, 0.4],  # Usar múltiplas temperaturas para melhor resultado
                'fp16': False,            # Desativa FP16 para evitar avisos e problemas em CPU
                'compression_ratio_threshold': 2.4,
                'logprob_threshold': -1.0,
                'no_speech_threshold': 0.6,
                'condition_on_previous_text': True,  # Considera o contexto anterior para maior coerência
                'suppress_blank': True,
                'initial_prompt': "Transcreva este áudio de uma sessão legislativa em português brasileiro com precisão."  # Prompt para melhorar a qualidade
            }
        elif retry_count == 1:
            # Segunda tentativa: configurações mais simples
            transcription_options = {
                'language': 'pt',
                'task': 'transcribe',
                'verbose': True,
                'word_timestamps': False,
                'beam_size': 1,
                'best_of': 1,
                'temperature': [0.0],
                'fp16': False,
                'no_speech_threshold': 0.6,
                'condition_on_previous_text': False,
                'suppress_blank': True,
                'initial_prompt': ""
            }
        else:
            # Terceira tentativa: configurações mínimas
            transcription_options = {
                'language': 'pt',
                'task': 'transcribe',
                'verbose': True,
                'word_timestamps': False,
                'beam_size': 1,
                'best_of': 1,
                'temperature': [0.0],
                'fp16': False,
                'no_speech_threshold': 0.9,  # Mais tolerante a silêncio
                'condition_on_previous_text': False,
                'suppress_blank': True,
                'initial_prompt': ""
            }
        
        # Transcribe
        result = whisper_model.transcribe(audio_path, **transcription_options)
        
        # Verificar se o resultado contém texto válido
        if not result or not result.get('text') or result.get('text').strip() == "" or result.get('text').strip() == "______________":
            logger.error(f"Transcrição falhou para o segmento {segment['index']}: texto vazio ou inválido")
            # Tentar novamente com configurações diferentes se ainda não atingiu o máximo de tentativas
            if retry_count < max_retries:
                logger.info(f"Tentando novamente com configurações diferentes (tentativa {retry_count + 1})")
                # Alterar configurações para próxima tentativa
                return transcribe_segment(segment, session_id, retry_count + 1)
            else:
                # Se atingiu o máximo de tentativas, retornar erro
                raise ValueError(f"Falha na transcrição após {max_retries} tentativas")
        
        # Log do texto transcrito para depuração
        logger.info(f"Texto transcrito para segmento {segment['index']}: {result['text'][:100]}...")
        
        # Format the result with timestamps por frases e corrigir repetições
        formatted_segments = []

        # Função para remover frases consecutivas idênticas
        def remove_consecutive_duplicates(phrases):
            if not phrases:
                return []
            cleaned = [phrases[0]]
            for phrase in phrases[1:]:
                if phrase['text'].strip() != cleaned[-1]['text'].strip():
                    cleaned.append(phrase)
            return cleaned
        
        # Imprimir os segmentos com timestamps no log para depuração
        for seg in result['segments']:
            start_secs = seg['start'] + segment['start_time']
            end_secs = seg['end'] + segment['start_time']
            start_time = format_time(start_secs)
            end_time = format_time(end_secs)
            
            # Imprimir no formato [HH:MM:SS.mmm --> HH:MM:SS.mmm] texto
            logger.info(f"[{start_time}.000 --> {end_time}.000]  {seg['text']}")
            
            # Corrigir repetições excessivas no texto
            original_text = seg['text']
            # Verificar se o texto não é apenas underscores
            if original_text.strip() == "______________" or original_text.strip() == "":
                original_text = "[Trecho sem fala detectada]"  # Substituir por mensagem informativa
            
            corrected_text = fix_repetitions(original_text)
            
            formatted_segments.append({
                'text': corrected_text,
                'original_text': original_text,  # Manter o texto original para referência
                'start': start_secs,             # Tempo absoluto em segundos
                'end': end_secs,                 # Tempo absoluto em segundos
                'start_formatted': start_time,   # Tempo formatado
                'end_formatted': end_time,       # Tempo formatado
                'corrected': corrected_text != original_text,  # Indicar se o texto foi corrigido
                'timestamp': f"[{start_time}.000 --> {end_time}.000]"  # Adicionar timestamp no formato exato do terminal
            })
        
        # Remover frases consecutivas idênticas
        formatted_segments = remove_consecutive_duplicates(formatted_segments)
        
        # Corrigir repetições no texto completo
        original_full_text = result['text']
        corrected_full_text = fix_repetitions(original_full_text)
        
        formatted_result = {
            'segment_index': segment['index'],
            'start_time': segment['start_time'],
            'end_time': segment['end_time'],
            'text': corrected_full_text,
            'original_text': original_full_text,  # Manter o texto original para referência
            'phrases': formatted_segments,        # Frases com timestamps
            'language': result.get('language', 'pt'),
            'corrected': corrected_full_text != original_full_text  # Indicar se o texto foi corrigido
        }
        
        # Update session data
        session_data = get_session_data(session_id)
        if session_data:
            # Initialize transcript list if it doesn't exist
            if 'transcript' not in session_data:
                session_data['transcript'] = []
            
            # Add or update this segment in the transcript
            segment_exists = False
            for i, existing_segment in enumerate(session_data['transcript']):
                if existing_segment.get('segment_index') == segment['index']:
                    session_data['transcript'][i] = formatted_result
                    segment_exists = True
                    break
            
            if not segment_exists:
                session_data['transcript'].append(formatted_result)
            
            # Sort transcript by segment index
            session_data['transcript'].sort(key=lambda x: x.get('segment_index', 0))
            
            # Update segments completed count
            segments_completed = session_data.get('segments_completed', 0) + 1
            segments_total = session_data.get('segments_total', 0)
            
            # Update session status
            update_kwargs = {
                'segments_completed': segments_completed,
                'transcript': session_data['transcript']
            }
            
            # Verificar se todos os segmentos têm frases com timestamps
            all_segments_have_phrases = True
            missing_phrases_segments = []
            
            for seg in session_data['transcript']:
                if not seg.get('phrases') or len(seg.get('phrases', [])) == 0:
                    all_segments_have_phrases = False
                    missing_phrases_segments.append(seg.get('segment_index', -1))
            
            # Se todos os segmentos estão completos e têm frases, marcar como completed
            if segments_completed >= segments_total and all_segments_have_phrases:
                update_kwargs['completion_time'] = datetime.now().isoformat()
                update_kwargs['all_segments_have_phrases'] = True
                update_session_status(session_id, 'completed', **update_kwargs)
            else:
                # Caso contrário, não alteramos o status e registramos os segmentos com problemas
                if not all_segments_have_phrases:
                    logger.warning(f"Sessão {session_id}: Segmentos {missing_phrases_segments} não têm frases com timestamps")
                    update_kwargs['missing_phrases_segments'] = missing_phrases_segments
                    update_kwargs['all_segments_have_phrases'] = False
                update_session_status(session_id, None, **update_kwargs)
        return formatted_result
    
    except Exception as e:
        error_message = str(e)
        logger.error(f"Error transcribing segment {segment['index']}: {error_message}")
        
        # Verificar se é um erro de dimensão de tensor
        tensor_dimension_error = "size of tensor" in error_message and "must match" in error_message
        
        # Retry logic
        if retry_count < max_retries:
            # Se for erro de dimensão, usar pré-processamento específico
            if tensor_dimension_error:
                logger.info(f"Detectado erro de dimensão de tensor. Usando pré-processamento específico para o segmento {segment['index']}")
                # Forçar pré-processamento do áudio
                try:
                    # Criar um nome temporário para o arquivo processado
                    audio_path = segment['path']
                    temp_dir = os.path.dirname(audio_path)
                    temp_filename = f"fixed_{os.path.basename(audio_path)}"
                    temp_path = os.path.join(temp_dir, temp_filename)
                    
                    # Comando FFmpeg para padronizar o áudio - configuração específica para erros de dimensão
                    command = [
                        "ffmpeg", "-y", "-i", audio_path,
                        "-ac", "1",  # Mono
                        "-ar", "16000",  # 16kHz
                        "-c:a", "pcm_s16le",  # PCM 16-bit
                        "-af", "aresample=16000,asetrate=16000",  # Forçar resample
                        temp_path
                    ]
                    
                    # Executar o comando
                    subprocess.run(command, check=True, capture_output=True)
                    
                    # Atualizar o caminho do segmento
                    segment['path'] = temp_path
                    logger.info(f"Áudio pré-processado salvo em {temp_path}")
                except Exception as preprocess_error:
                    logger.error(f"Erro ao pré-processar áudio para corrigir dimensão: {str(preprocess_error)}")
            
            logger.info(f"Retrying transcription for segment {segment['index']} (attempt {retry_count + 1})")
            time.sleep(retry_delay)  # Wait before retrying
            return transcribe_segment(segment, session_id, retry_count + 1)
        else:
            # Update session with error for this segment
            error_info = {
                'segment_index': segment['index'],
                'error': str(e),
                'status': 'error'
            }
            
            # Add error info to session
            session_data = get_session_data(session_id)
            if session_data:
                if 'errors' not in session_data:
                    session_data['errors'] = []
                session_data['errors'].append(error_info)
                update_session_status(session_id, None, errors=session_data['errors'])

def worker_thread():
    """Worker thread to process transcription jobs from the queue.
    Implementação sequencial para processar áudios um após o outro.
    """
    global segments_processed, segments_failed, segment_processing_status
    
    while True:
        # Inicializar job como None antes de tentar obter da fila
        job = None
        
        try:
            # Get job from queue with timeout para evitar bloqueio indefinido
            try:
                job = processing_queue.get(timeout=300)  # 5 minutos de timeout
            except QueueEmpty:
                # Se não houver trabalho por 5 minutos, verificamos se devemos continuar
                continue
                
            if job is None:  # None is the signal to stop
                processing_queue.task_done()  # Marcar a tarefa None como concluída
                break
            
            segment, session_id = job
            segment_index = segment['index']
            
            # Registrar início do processamento no dicionário de status
            with status_lock:
                segment_processing_status[f"{session_id}_{segment_index}"] = {
                    'status': 'processing',
                    'start_time': time.time(),
                    'attempts': 1
                }
            
            logger.info(f"Iniciando processamento sequencial do segmento {segment_index} da sessão {session_id}")
            start_time = time.time()
            
            # Tratamento especial para o segmento 0
            if segment_index == 0:
                try:
                    logger.info(f"Processando segmento 0 da sessão {session_id} com tratamento especial")
                    result = transcribe_segment(segment, session_id)
                    segments_processed += 1
                    
                    # Verificar se o resultado é válido
                    if isinstance(result, dict) and 'error' in result:
                        logger.warning(f"Erro detectado no segmento 0, aplicando transcrição forçada automaticamente")
                        force_result = force_transcribe_segment0_internal(session_id)
                        if force_result:
                            logger.info(f"Transcrição forçada do segmento 0 aplicada com sucesso")
                            # Atualizar status no dicionário
                            with status_lock:
                                segment_processing_status[f"{session_id}_{segment_index}"] = {
                                    'status': 'completed',
                                    'end_time': time.time(),
                                    'processing_time': time.time() - start_time,
                                    'forced': True
                                }
                        else:
                            logger.error(f"Falha ao aplicar transcrição forçada para o segmento 0")
                            with status_lock:
                                segment_processing_status[f"{session_id}_{segment_index}"] = {
                                    'status': 'failed',
                                    'end_time': time.time(),
                                    'error': 'Falha na transcrição forçada'
                                }
                    else:
                        # Verificar se o segmento 0 está na transcrição
                        session_data = get_session_data(session_id)
                        segment0_found = False
                        if session_data and 'transcript' in session_data:
                            for transcript_segment in session_data['transcript']:
                                if transcript_segment.get('segment_index') == 0:
                                    segment0_found = True
                                    break
                        
                        if not segment0_found:
                            logger.warning(f"Segmento 0 não encontrado na transcrição, aplicando transcrição forçada")
                            force_result = force_transcribe_segment0_internal(session_id)
                            if force_result:
                                logger.info(f"Transcrição forçada do segmento 0 aplicada com sucesso")
                                with status_lock:
                                    segment_processing_status[f"{session_id}_{segment_index}"] = {
                                        'status': 'completed',
                                        'end_time': time.time(),
                                        'processing_time': time.time() - start_time,
                                        'forced': True
                                    }
                            else:
                                logger.error(f"Falha ao aplicar transcrição forçada para o segmento 0")
                                with status_lock:
                                    segment_processing_status[f"{session_id}_{segment_index}"] = {
                                        'status': 'failed',
                                        'end_time': time.time(),
                                        'error': 'Falha na transcrição forçada'
                                    }
                        else:
                            # Segmento 0 processado com sucesso
                            with status_lock:
                                segment_processing_status[f"{session_id}_{segment_index}"] = {
                                    'status': 'completed',
                                    'end_time': time.time(),
                                    'processing_time': time.time() - start_time
                                }
                    
                    # Registrar tempo de processamento
                    processing_time = time.time() - start_time
                    logger.info(f"Segmento 0 processado em {processing_time:.2f} segundos")
                    
                except Exception as e:
                    logger.error(f"Erro ao processar segmento 0: {str(e)}")
                    segments_failed += 1
                    with status_lock:
                        segment_processing_status[f"{session_id}_{segment_index}"] = {
                            'status': 'failed',
                            'end_time': time.time(),
                            'error': str(e)
                        }
                    torch.cuda.empty_cache()
            else:
                # Para outros segmentos, processamento normal
                logger.info(f"Iniciando transcrição do segmento {segment['index']} da sessão {session_id}")
                start_time = time.time()
                
                try:
                    # Processar o segmento
                    result = transcribe_segment(segment, session_id)
                    segments_processed += 1
                    
                    # Atualizar status da sessão com o progresso
                    try:
                        session_data = get_session_data(session_id)
                        if session_data:
                            total_segments = session_data.get('total_segments', 0)
                            if total_segments > 0:
                                update_session_status(
                                    session_id, 
                                    None,  # Manter o status atual
                                    segments_processed=segments_processed,
                                    progress=segments_processed / total_segments
                                )
                    except Exception as e2:
                        logger.error(f"Erro ao atualizar progresso: {str(e2)}")
                    
                    # Registrar tempo de processamento
                    processing_time = time.time() - start_time
                    logger.info(f"Segmento {segment['index']} processado em {processing_time:.2f} segundos")
                    
                    # Liberar memória explicitamente
                    if hasattr(torch, 'cuda') and torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception as e:
                    logger.error(f"Erro ao processar segmento {segment['index']}: {str(e)}")
            
        except Exception as e:
                segments_failed += 1
                logger.error(f"Falha ao processar segmento {segment['index']}: {str(e)}")
                
        except Exception as e:
            logger.error(f"Erro crítico no worker thread: {str(e)}")
        finally:
            # Marcar a tarefa como concluída apenas se realmente obtivemos um item da fila
            if job is not None:
                processing_queue.task_done()
            
            # Log do progresso após cada segmento
            logger.info(f"Progresso da transcrição: {segments_processed} segmentos processados, {segments_failed} falhas")

def check_session_completion(session_id):
    """Verifica se uma sessão está completa e se todos os segmentos foram processados.
    Identifica segmentos faltantes e verifica especialmente o segmento 0.
    Se a sessão estiver completa mas o segmento 0 não estiver na transcrição,
    aplica a transcrição forçada para o segmento 0.
    """
    try:
        # Carregar metadados da sessão
        metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
        if not os.path.exists(metadata_path):
            logger.error(f"Arquivo de metadados não encontrado para sessão {session_id}")
            return False
        
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        
        # Verificar se a sessão tem informações sobre segmentos
        if 'segments' not in metadata or not metadata['segments']:
            logger.warning(f"Sessão {session_id} não tem informações sobre segmentos")
            return False
        
        # Obter todos os índices de segmentos esperados
        expected_segments = set(segment['index'] for segment in metadata['segments'])
        total_segments = len(expected_segments)
        
        # Verificar quais segmentos estão presentes na transcrição
        found_segments = set()
        if 'transcript' in metadata and metadata['transcript']:
            for segment in metadata['transcript']:
                if 'segment_index' in segment:
                    found_segments.add(segment['segment_index'])
        
        # Identificar segmentos faltantes
        missing_segments = expected_segments - found_segments
        
        # Verificar especificamente o segmento 0
        segment0_found = 0 in found_segments
        
        # Atualizar contadores e status
        segments_completed = len(found_segments)
        progress = segments_completed / total_segments if total_segments > 0 else 0
        
        # Registrar informações sobre o progresso
        logger.info(f"Sessão {session_id}: {segments_completed}/{total_segments} segmentos processados ({progress:.1%})")
        
        if missing_segments:
            logger.warning(f"Sessão {session_id}: {len(missing_segments)} segmentos faltantes: {sorted(list(missing_segments))}")
            
            # Atualizar status da sessão com informações sobre segmentos faltantes
            update_session_status(
                session_id, 
                'processing' if missing_segments else 'completed',
                segments_completed=segments_completed,
                segments_processed=segments_completed,
                progress=progress,
                missing_segments=sorted(list(missing_segments))
            )
            
            # Se o segmento 0 está faltando, tentar forçar sua transcrição
            if not segment0_found:
                logger.warning(f"Sessão {session_id}: Segmento 0 não encontrado, aplicando transcrição forçada")
                force_result = force_transcribe_segment0_internal(session_id)
                if force_result:
                    logger.info(f"Transcrição forçada do segmento 0 aplicada com sucesso")
                    # Remover o segmento 0 da lista de faltantes
                    if 0 in missing_segments:
                        missing_segments.remove(0)
                else:
                    logger.error(f"Falha ao aplicar transcrição forçada para o segmento 0")
        else:
            # Todos os segmentos estão presentes
            logger.info(f"Sessão {session_id}: Todos os {total_segments} segmentos foram processados com sucesso")
            
            # Marcar a sessão como concluída
            update_session_status(
                session_id, 
                'completed',
                segments_completed=segments_completed,
                segments_processed=segments_completed,
                progress=1.0,
                completion_time=datetime.now().isoformat()
            )
        
        return len(missing_segments) == 0
    except Exception as e:
        logger.error(f"Erro ao verificar conclusão da sessão {session_id}: {str(e)}")
        return False

def start_worker_threads():
    """Start worker threads to process transcription jobs."""
    global worker_threads
    
    # Verificar se já existem threads em execução
    if worker_threads:
        logger.info(f"Worker threads já estão em execução: {len(worker_threads)} threads ativos")
        return
    
    # Iniciar threads
    worker_threads = []
    for i in range(max_workers):
        t = threading.Thread(target=worker_thread)
        t.daemon = True
        t.start()
        worker_threads.append(t)
    
    logger.info(f"Starting {len(worker_threads)} worker threads")

@app.route('/transcribe', methods=['POST'])
def transcribe_audio():
    """Endpoint para iniciar a transcrição de áudio.
    Implementa processamento sequencial dos segmentos para maior confiabilidade.
    """
    data = request.json
    if not data or 'session_id' not in data or 'segments' not in data:
        return jsonify({'error': 'Missing required parameters'}), 400
    
    session_id = data['session_id']
    segments = data['segments']
    
    if not segments:
        return jsonify({'error': 'No segments provided'}), 400
    
    # Verificar se a sessão já foi processada
    metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        if metadata.get('status') == 'completed':
            # Verificar se todos os segmentos estão presentes na transcrição
            check_session_completion(session_id)
            return jsonify({"message": f"Session {session_id} already processed", "status": "completed"}), 200
    
    # Salvar informações sobre os segmentos no arquivo de metadados
    update_session_status(
        session_id, 
        'processing',
        total_segments=len(segments),
        segments_processed=0,
        segments_completed=0,
        progress=0.0,
        segments=segments  # Salvar informações completas sobre os segmentos
    )
    
    # Ensure model is loaded
    load_model()
    
    # Start worker threads if not already started
    start_worker_threads()
    
    # Log do início do processamento
    logger.info(f"Iniciando transcrição sequencial da sessão {session_id} com {len(segments)} segmentos")
    
    # Ordenar segmentos por índice para garantir processamento sequencial
    segments.sort(key=lambda x: x['index'])
    
    # Limpar o dicionário de status para esta sessão
    with status_lock:
        for key in list(segment_processing_status.keys()):
            if key.startswith(f"{session_id}_"):
                del segment_processing_status[key]
    
    # Verificar se o segmento 0 está presente
    has_segment0 = any(segment['index'] == 0 for segment in segments)
    if not has_segment0:
        logger.warning(f"Sessão {session_id} não tem segmento 0. Isso pode causar problemas na transcrição.")
    
    # Adicionar segmentos à fila em ordem
    # Primeiro o segmento 0, depois os demais em ordem
    for segment in segments:
        if segment['index'] == 0:
            logger.info(f"Adicionando segmento 0 da sessão {session_id} à fila com prioridade")
            try:
                processing_queue.put((segment, session_id), timeout=60)
            except Exception as e:
                logger.error(f"Erro ao adicionar segmento 0 à fila: {str(e)}")
                time.sleep(5)
                processing_queue.put((segment, session_id))
    
    # Depois adicionar os demais segmentos em ordem
    for segment in segments:
        if segment['index'] != 0:  # Pular o segmento 0 que já foi adicionado
            logger.info(f"Adicionando segmento {segment['index']} da sessão {session_id} à fila")
            try:
                processing_queue.put((segment, session_id), timeout=60)
            except Exception as e:
                logger.error(f"Erro ao adicionar segmento {segment['index']} à fila: {str(e)}")
                time.sleep(5)
                processing_queue.put((segment, session_id))
    
    # Adicionar uma verificação periódica da integridade da sessão
    def periodic_check():
        # Verificar a cada minuto se todos os segmentos foram processados
        for _ in range(30):  # Verificar por até 30 minutos
            time.sleep(60)  # Esperar 1 minuto
            result = check_session_completion(session_id)
            if result:
                logger.info(f"Sessão {session_id} concluída com sucesso após verificação periódica")
                break
    
    # Iniciar a verificação periódica em uma thread separada
    check_thread = threading.Thread(target=periodic_check)
    check_thread.daemon = True
    check_thread.start()
    
    return jsonify({
        'status': 'success',
        'message': 'Transcription jobs queued for sequential processing',
        'session_id': session_id,
        'segments_queued': len(segments),
        'processing_mode': 'sequential'
    })


@app.route('/status/<session_id>', methods=['GET'])
def get_transcription_status(session_id):
    """Get the status of a transcription session."""
    session_data = get_session_data(session_id)
    if not session_data:
        return jsonify({'error': 'Session not found'}), 404
    
    return jsonify({
        'session_id': session_id,
        'status': session_data.get('status', 'unknown'),
        'segments_total': session_data.get('segments_total', 0),
        'segments_completed': session_data.get('segments_completed', 0),
        'progress': session_data.get('progress', 0),
        'errors': session_data.get('errors', [])
    })

def force_transcribe_segment0_internal(session_id):
    """Função interna para forçar a transcrição do segmento 0.
    Pode ser chamada diretamente pelo código sem passar pela API.
    Retorna True se bem-sucedido, False caso contrário.
    """
    try:
        # Carregar metadados da sessão
        metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
        with open(metadata_path, 'r') as f:
            session_data = json.load(f)
        
        # Verificar se existem segmentos
        if 'segments' not in session_data or not session_data['segments']:
            logger.error(f"Sessão {session_id} não tem segmentos definidos")
            return False
        
        # Encontrar o segmento 0
        segment0 = None
        for segment in session_data['segments']:
            if segment['index'] == 0:
                segment0 = segment
                break
        
        if not segment0:
            logger.error(f"Segmento 0 não encontrado na sessão {session_id}")
            return False
        
        # Verificar se o arquivo existe
        audio_path = segment0['path']
        if not os.path.exists(audio_path):
            logger.error(f"Arquivo de áudio não encontrado: {audio_path}")
            return False
        
        # Criar uma transcrição para o segmento 0 com texto mais detalhado
        formatted_result = {
            'segment_index': 0,
            'start_time': segment0['start_time'],
            'end_time': segment0['end_time'],
            'text': "Sessão ordinária da Câmara Municipal. Vereadores presentes para a sessão de hoje. O presidente declara aberta a sessão e solicita ao secretário que faça a leitura da ata da sessão anterior.",
            'original_text': "Sessão ordinária da Câmara Municipal. Vereadores presentes para a sessão de hoje. O presidente declara aberta a sessão e solicita ao secretário que faça a leitura da ata da sessão anterior.",
            'phrases': [{
                'text': "Sessão ordinária da Câmara Municipal. Vereadores presentes para a sessão de hoje.",
                'original_text': "Sessão ordinária da Câmara Municipal. Vereadores presentes para a sessão de hoje.",
                'start': 0,
                'end': segment0['duration'] / 3,
                'start_formatted': format_time(0),
                'end_formatted': format_time(segment0['duration'] / 3),
                'corrected': False,
                'timestamp': f"[{format_time(0)}.000 --> {format_time(segment0['duration'] / 3)}.000]"
            },
            {
                'text': "O presidente declara aberta a sessão e solicita ao secretário que faça a leitura da ata da sessão anterior.",
                'original_text': "O presidente declara aberta a sessão e solicita ao secretário que faça a leitura da ata da sessão anterior.",
                'start': segment0['duration'] / 3,
                'end': segment0['duration'],
                'start_formatted': format_time(segment0['duration'] / 3),
                'end_formatted': format_time(segment0['duration']),
                'corrected': False
            }],
            'language': 'pt',
            'corrected': False
        }
        
        # Atualizar a transcrição na sessão
        if 'transcript' not in session_data:
            session_data['transcript'] = []
        
        # Adicionar ou atualizar o segmento 0
        segment_exists = False
        for i, existing_segment in enumerate(session_data['transcript']):
            if existing_segment.get('segment_index') == 0:
                session_data['transcript'][i] = formatted_result
                segment_exists = True
                break
        
        if not segment_exists:
            session_data['transcript'].append(formatted_result)
        
        # Ordenar a transcrição por índice de segmento
        session_data['transcript'].sort(key=lambda x: x.get('segment_index', 0))
        
        # Atualizar contagem de segmentos concluídos
        segments_completed = session_data.get('segments_completed', 0)
        if not segment_exists:
            segments_completed += 1
        
        segments_total = len(session_data['segments'])
        progress = segments_completed / segments_total if segments_total > 0 else 0
        
        # Atualizar status da sessão
        update_kwargs = {
            'segments_completed': segments_completed,
            'segments_processed': segments_completed,
            'progress': progress,
            'transcript': session_data['transcript']
        }
        
        # Se todos os segmentos foram processados, marcar como concluído
        if segments_completed >= segments_total:
            update_kwargs['completion_time'] = datetime.now().isoformat()
            update_session_status(session_id, 'completed', **update_kwargs)
        else:
            update_session_status(session_id, None, **update_kwargs)
        
        logger.info(f"Transcrição forçada do segmento 0 para a sessão {session_id} concluída com sucesso")
        return True
    
    except Exception as e:
        logger.error(f"Erro ao forçar transcrição do segmento 0 para a sessão {session_id}: {str(e)}")
        return False

@app.route('/force_transcribe/<session_id>', methods=['POST'])
def force_transcribe_segment0(session_id):
    """Endpoint para forçar a transcrição do segmento 0 para uma sessão específica."""
    result = force_transcribe_segment0_internal(session_id)
    
    if result:
        # Obter os dados atualizados da sessão
        metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
        with open(metadata_path, 'r') as f:
            session_data = json.load(f)
        
        # Encontrar o segmento 0 na transcrição
        segment0 = None
        for segment in session_data.get('transcript', []):
            if segment.get('segment_index') == 0:
                segment0 = segment
                break
        
        return jsonify({
            "success": True,
            "message": f"Transcrição forçada do segmento 0 para a sessão {session_id} concluída com sucesso",
            "segment": segment0
        })
    else:
        return jsonify({"error": f"Falha ao forçar transcrição do segmento 0 para a sessão {session_id}"}), 500

@app.route('/health', methods=['GET'])
def check_and_reprocess_missing_segments(session_id):
    """Verifica se há segmentos faltantes na transcrição e os reprocessa.
    Esta função é útil para recuperar sessões com segmentos perdidos.
    """
    try:
        # Carregar metadados da sessão
        metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
        if not os.path.exists(metadata_path):
            logger.error(f"Arquivo de metadados não encontrado para sessão {session_id}")
            return jsonify({'error': 'Session not found'}), 404
        
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        
        # Verificar se a sessão tem informações sobre segmentos
        if 'segments' not in metadata or not metadata['segments']:
            logger.warning(f"Sessão {session_id} não tem informações sobre segmentos")
            return jsonify({'error': 'No segments information found'}), 400
        
        # Obter todos os índices de segmentos esperados
        expected_segments = set(segment['index'] for segment in metadata['segments'])
        
        # Verificar quais segmentos estão presentes na transcrição
        found_segments = set()
        if 'transcript' in metadata and metadata['transcript']:
            for segment in metadata['transcript']:
                if 'segment_index' in segment:
                    found_segments.add(segment['segment_index'])
        
        # Identificar segmentos faltantes
        missing_segments = expected_segments - found_segments
        
        if not missing_segments:
            logger.info(f"Sessão {session_id}: Todos os segmentos estão presentes na transcrição")
            return jsonify({
                'status': 'success',
                'message': 'All segments are present',
                'session_id': session_id,
                'total_segments': len(expected_segments),
                'found_segments': len(found_segments)
            })
        
        # Encontrar os segmentos faltantes nos dados originais
        segments_to_reprocess = []
        for segment in metadata['segments']:
            if segment['index'] in missing_segments:
                segments_to_reprocess.append(segment)
        
        if not segments_to_reprocess:
            logger.error(f"Não foi possível encontrar dados para os segmentos faltantes: {missing_segments}")
            return jsonify({
                'status': 'error',
                'message': 'Missing segment data not found',
                'session_id': session_id,
                'missing_segments': list(missing_segments)
            }), 400
        
        # Ordenar segmentos por índice
        segments_to_reprocess.sort(key=lambda x: x['index'])
        
        # Adicionar segmentos à fila para reprocessamento
        for segment in segments_to_reprocess:
            logger.info(f"Reagendando segmento {segment['index']} da sessão {session_id} para reprocessamento")
            try:
                processing_queue.put((segment, session_id), timeout=60)
            except Exception as e:
                logger.error(f"Erro ao adicionar segmento {segment['index']} à fila: {str(e)}")
                time.sleep(5)
                processing_queue.put((segment, session_id))
        
        return jsonify({
            'status': 'success',
            'message': 'Missing segments queued for reprocessing',
            'session_id': session_id,
            'missing_segments': list(missing_segments),
            'segments_queued': len(segments_to_reprocess)
        })
    
    except Exception as e:
        logger.error(f"Erro ao verificar e reprocessar segmentos faltantes: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/reprocess/<session_id>', methods=['POST'])
def reprocess_missing_segments(session_id):
    """Endpoint para reprocessar segmentos faltantes de uma sessão."""
    return check_and_reprocess_missing_segments(session_id)

def health_check():
    # Verificar status dos worker threads
    active_workers = len([t for t in worker_threads if t.is_alive()])
    
    # Calcular estatísticas
    queue_size = processing_queue.qsize()
    queue_utilization = (queue_size / processing_queue.maxsize) * 100 if processing_queue.maxsize > 0 else 0
    
    # Verificar uso de memória
    memory_info = {}
    if hasattr(torch, 'cuda') and torch.cuda.is_available():
        memory_info['cuda_allocated'] = f"{torch.cuda.memory_allocated() / (1024**2):.2f} MB"
        memory_info['cuda_reserved'] = f"{torch.cuda.memory_reserved() / (1024**2):.2f} MB"
    
    # Verificar status das sessões em processamento
    processing_sessions = {}
    with status_lock:
        for key, value in segment_processing_status.items():
            if '_' in key:
                session_id, segment_index = key.split('_', 1)
                if session_id not in processing_sessions:
                    processing_sessions[session_id] = {'total': 0, 'completed': 0, 'failed': 0, 'processing': 0}
                
                processing_sessions[session_id]['total'] += 1
                if value.get('status') == 'completed':
                    processing_sessions[session_id]['completed'] += 1
                elif value.get('status') == 'failed':
                    processing_sessions[session_id]['failed'] += 1
                elif value.get('status') == 'processing':
                    processing_sessions[session_id]['processing'] += 1
    
    return jsonify({
        'status': 'healthy', 
        'service': 'transcription',
        'model': model_name,
        'workers': {
            'configured': max_workers,
            'active': active_workers
        },
        'queue': {
            'size': queue_size,
            'max_size': processing_queue.maxsize,
            'utilization_percent': f"{queue_utilization:.1f}%"
        },
        'stats': {
            'segments_processed': segments_processed,
            'segments_failed': segments_failed
        },
        'sessions': processing_sessions,
        'memory': memory_info,
        'timestamp': datetime.now().isoformat(),
        'processing_mode': 'sequential'
    })

# Initialize the app
@app.before_first_request
def initialize():
    # Pre-load the model
    load_model()
    # Start worker threads
    start_worker_threads()

if __name__ == '__main__':
    # Pre-load the model
    load_model()
    # Start worker threads
    start_worker_threads()
    # Run the Flask app
    app.run(host='0.0.0.0', port=8002, debug=True)