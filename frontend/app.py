import os
import json
import requests
import logging
import re
import io
import os
import tempfile
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_from_directory, Response, send_file
from werkzeug.utils import secure_filename
import uuid
from datetime import datetime
from docx import Document
from docx.shared import Pt, Inches

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_for_session_sync')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max upload size

# Usar caminhos de ambiente ou caminhos padrão locais
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/app/uploads')
app.config['DATA_FOLDER'] = os.environ.get('DATA_FOLDER', '/app/data')

# Service URLs from environment variables
PREPROCESSING_SERVICE_URL = os.environ.get('PREPROCESSING_SERVICE_URL', 'http://localhost:5001')
TRANSCRIPTION_SERVICE_URL = os.environ.get('TRANSCRIPTION_SERVICE_URL', 'http://localhost:5002')

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['DATA_FOLDER'], exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'audio_file' not in request.files:
        flash('Nenhum arquivo selecionado')
        return redirect(url_for('index'))
    
    file = request.files['audio_file']
    if file.filename == '':
        flash('Nenhum arquivo selecionado')
        return redirect(url_for('index'))
    
    if file:
        # Generate a unique session ID
        session_id = str(uuid.uuid4())
        
        # Create session directory
        session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
        os.makedirs(session_dir, exist_ok=True)
        
        # Save the file
        filename = secure_filename(file.filename)
        file_path = os.path.join(session_dir, filename)
        file.save(file_path)
        
        # Collect metadata
        metadata = {
            'session_id': session_id,
            'original_filename': filename,
            'upload_time': datetime.now().isoformat(),
            'title': request.form.get('title', ''),
            'date': request.form.get('date', ''),
            'description': request.form.get('description', ''),
            'file_path': file_path,
            'status': 'uploaded',
            
            # Campos para estruturação da ata
            'ata': {
                'numero_sessao': request.form.get('numero_sessao', ''),
                'periodo': request.form.get('periodo', ''),
                'numero_sessao_legislativa': request.form.get('numero_sessao_legislativa', ''),
                'numero_legislatura': request.form.get('numero_legislatura', ''),
                'cidade': request.form.get('cidade', ''),
                'hora': request.form.get('hora', ''),
                'minutos': request.form.get('minutos', ''),
                'presidente': request.form.get('presidente', ''),
                'primeiro_secretario': request.form.get('primeiro_secretario', ''),
                'segundo_secretario': request.form.get('segundo_secretario', ''),
                'vereadores_presentes': request.form.get('vereadores_presentes', ''),
                'numero_presentes': request.form.get('numero_presentes', ''),
                'numero_presentes_extenso': ''
            }
        }
        
        # Converter número de presentes para extenso se fornecido
        if metadata['ata']['numero_presentes']:
            try:
                num = int(metadata['ata']['numero_presentes'])
                # Função simplificada para converter números para extenso em português
                unidades = ['zero', 'um', 'dois', 'três', 'quatro', 'cinco', 'seis', 'sete', 'oito', 'nove', 'dez',
                           'onze', 'doze', 'treze', 'quatorze', 'quinze', 'dezesseis', 'dezessete', 'dezoito', 'dezenove']
                dezenas = ['', '', 'vinte', 'trinta', 'quarenta', 'cinquenta']
                
                if num < 20:
                    metadata['ata']['numero_presentes_extenso'] = unidades[num]
                elif num < 60:
                    if num % 10 == 0:
                        metadata['ata']['numero_presentes_extenso'] = dezenas[num // 10]
                    else:
                        metadata['ata']['numero_presentes_extenso'] = f"{dezenas[num // 10]} e {unidades[num % 10]}"
            except ValueError:
                pass  # Ignora se não for um número válido
        
        # Save metadata
        metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        # Send to preprocessing service
        try:
            # Primeiro salvamos os metadados e redirecionamos o usuário
            flash('Arquivo enviado com sucesso e está sendo processado')
            
            # Redirecionar para a página de status imediatamente
            redirect_url = url_for('session_status_endpoint', session_id=session_id)
            
            # Enviar para o serviço de pré-processamento
            try:
                # Enviar o caminho do arquivo para o serviço de pré-processamento
                response = requests.post(
                    f"{PREPROCESSING_SERVICE_URL}/preprocess",
                    json={
                        'session_id': session_id,
                        'file_path': file_path
                    },
                    timeout=5  # Timeout aumentado para permitir processamento de arquivos grandes
                )
                
                # Verificar resposta
                if response.status_code == 200:
                    logger.info(f"Pré-processamento iniciado com sucesso para sessão {session_id}")
                    # O serviço de pré-processamento já envia automaticamente para transcrição
                else:
                    logger.error(f"Erro no pré-processamento: {response.text}")
                    flash(f"Erro no pré-processamento: {response.text}")
            except (requests.RequestException, requests.Timeout) as e:
                # Apenas registrar o erro, mas continuar com o redirecionamento
                print(f"Aviso: Erro ao iniciar processamento (continuará em segundo plano): {str(e)}")
            
            # Redirecionar o usuário imediatamente
            return redirect(redirect_url)
        except requests.RequestException as e:
            flash(f'Erro ao conectar ao serviço de pré-processamento: {str(e)}')
            return redirect(url_for('index'))
    
    return redirect(url_for('index'))

@app.route('/sessions')
def list_sessions():
    sessions = []
    for filename in os.listdir(app.config['DATA_FOLDER']):
        if filename.endswith('.json'):
            with open(os.path.join(app.config['DATA_FOLDER'], filename), 'r') as f:
                session_data = json.load(f)
                sessions.append(session_data)
    
    return render_template('sessions.html', sessions=sessions)

@app.route('/session/<session_id>', endpoint='session_status_endpoint')
def session_status(session_id):
    session_data = get_session_data(session_id)
    if not session_data:
        flash('Sessão não encontrada')
        return redirect(url_for('index'))
    
    # Verificar se há transcrição disponível
    has_transcript = 'transcript' in session_data and session_data['transcript']
    
    return render_template(
        'session.html', 
        session=session_data, 
        session_id=session_id,
        has_transcript=has_transcript
    )

@app.route('/download_transcript/<session_id>')
def download_transcript(session_id):
    """Faz download da transcrição completa sem timestamps."""
    # Obter dados da sessão
    session_data = get_session_data(session_id)
    if not session_data or 'transcript' not in session_data or not session_data['transcript']:
        flash('Transcrição não encontrada')
        return redirect(url_for('session_status', session_id=session_id))
    
    # Extrair o texto completo da transcrição sem timestamps
    transcript_text = ""
    for segment in session_data['transcript']:
        if 'text' in segment:
            transcript_text += segment['text'] + "\n\n"
    
    # Criar um arquivo temporário para o texto
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='w', encoding='utf-8')
    temp_file.write(transcript_text)
    temp_file.close()
    
    # Enviar o arquivo para download
    return send_file(
        temp_file.name,
        as_attachment=True,
        download_name=f"transcript_{session_id}.txt",
        mimetype='text/plain'
    )

@app.route('/transcribe/<session_id>', methods=['POST'])
def start_transcription(session_id):
    """Inicia o processo de transcrição para uma sessão que já foi pré-processada"""
    session_data = get_session_data(session_id)
    if not session_data:
        return jsonify({'error': 'Sessão não encontrada'}), 404
    
    # Verificar se a sessão já foi pré-processada
    if session_data.get('status') != 'preprocessed':
        return jsonify({'error': 'Sessão ainda não foi pré-processada'}), 400
    
    try:
        # Enviar requisição para o serviço de transcrição
        response = requests.post(
            f"{TRANSCRIPTION_SERVICE_URL}/transcribe",
            json={
                'session_id': session_id
            },
            timeout=5
        )
        
        if response.status_code == 200:
            # Atualizar status da sessão
            session_data['status'] = 'transcribing'
            with open(os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json"), 'w') as f:
                json.dump(session_data, f, indent=2)
            
            return jsonify({
                'status': 'success',
                'message': 'Transcrição iniciada com sucesso'
            })
        else:
            return jsonify({
                'error': f'Erro ao iniciar transcrição: {response.text}'
            }), response.status_code
    
    except requests.RequestException as e:
        return jsonify({
            'error': f'Erro ao conectar ao serviço de transcrição: {str(e)}'
        }), 500

@app.route('/api/session/<session_id>', endpoint='get_session_api_status_endpoint')
def get_session_api_status(session_id):
    """Endpoint API para obter o status atual da sessão em formato JSON."""
    session_data = get_session_data(session_id)
    if not session_data:
        return jsonify({'error': 'Session not found'}), 404
    
    # Verificar status atual no serviço de transcrição se estiver em processamento
    if session_data.get('status') in ['preprocessing', 'transcribing', 'processing']:
        try:
            # Verificar status no serviço de transcrição
            response = requests.get(
                f"{TRANSCRIPTION_SERVICE_URL}/status/{session_id}",
                timeout=5
            )
            
            if response.status_code == 200:
                transcription_data = response.json()
                
                # Atualizar informações de progresso
                session_data['status'] = transcription_data.get('status', session_data.get('status'))
                session_data['segments_total'] = transcription_data.get('segments_total', 0)
                session_data['segments_completed'] = transcription_data.get('segments_completed', 0)
                session_data['progress'] = transcription_data.get('progress', 0)
                session_data['errors'] = transcription_data.get('errors', [])
                session_data['missing_segments'] = transcription_data.get('missing_segments', [])
                session_data['processing_mode'] = transcription_data.get('processing_mode', 'sequential')
                
                # Salvar atualizações no arquivo JSON
                metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
                with open(metadata_path, 'w') as f:
                    json.dump(session_data, f, indent=2)
        except requests.RequestException as e:
            logger.error(f"Erro ao verificar status da transcrição: {str(e)}")
            # Continuar com os dados locais em caso de erro
    
    # Retornar dados da sessão
    return jsonify(session_data)

@app.route('/api/reprocess/<session_id>', methods=['POST'])
def reprocess_missing_segments(session_id):
    """Endpoint para solicitar o reprocessamento de segmentos faltantes."""
    session_data = get_session_data(session_id)
    if not session_data:
        return jsonify({'error': 'Session not found'}), 404
    
    try:
        # Enviar solicitação para o serviço de transcrição
        response = requests.post(
            f"{TRANSCRIPTION_SERVICE_URL}/reprocess/{session_id}",
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"Reprocessamento iniciado para sessão {session_id}: {result}")
            return jsonify({
                'status': 'success',
                'message': 'Reprocessamento iniciado com sucesso',
                'details': result
            })
        else:
            logger.error(f"Erro ao iniciar reprocessamento: {response.text}")
            return jsonify({
                'status': 'error',
                'error': f"Erro no serviço de transcrição: {response.text}"
            }), 500
    except requests.RequestException as e:
        logger.error(f"Erro ao conectar ao serviço de transcrição: {str(e)}")
        return jsonify({
            'status': 'error',
            'error': f"Erro de conexão: {str(e)}"
        }), 500

@app.route('/uploads/<session_id>/<filename>')
def serve_audio(session_id, filename):
    """Serve os arquivos de áudio para o player."""
    return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], session_id), filename)

def get_session_data(session_id):
    """Obter dados da sessão a partir do arquivo JSON."""
    metadata_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
    
    if not os.path.exists(metadata_path):
        return None
    
    with open(metadata_path, 'r') as f:
        session_data = json.load(f)
    
    return session_data

@app.route('/api/session/analyze/<session_id>', endpoint='analyze_session_integrity')
def analyze_session_integrity(session_id):
    """Analisa o status da sessão verificando segmentos faltantes e frases sem timestamps.
    Esta função é usada internamente para verificar a integridade da transcrição.
    """
    session_data = get_session_data(session_id)
    
    if not session_data:
        return jsonify({'error': 'Session not found'}), 404
    
    # Verificar se todos os segmentos foram realmente transcritos
    response_data = dict(session_data)  # Criar uma cópia para não modificar o original
    
    # Calcular o progresso real baseado nos segmentos transcritos
    segments_total = session_data.get('segments_total', 0)
    
    # Verificar se a transcrição existe e está completa
    if 'transcript' in session_data and segments_total > 0:
        segments_processed = len(session_data['transcript'])
        
        # Verificar se todos os segmentos esperados estão presentes
        expected_indices = set(range(segments_total))
        actual_indices = set(seg.get('segment_index', -1) for seg in session_data['transcript'])
        
        # Verificar se há segmentos faltando
        missing_segments = expected_indices - actual_indices
        
        # Verificar se todos os segmentos têm frases com timestamps
        segments_without_phrases = []
        for seg in session_data.get('transcript', []):
            if not seg.get('phrases') or len(seg.get('phrases', [])) == 0:
                segments_without_phrases.append(seg.get('segment_index', -1))
        
        # Adicionar informações detalhadas ao response
        response_data['segments_processed'] = segments_processed
        response_data['total_segments'] = segments_total
        response_data['progress'] = segments_processed / segments_total if segments_total > 0 else 0
        response_data['missing_segments'] = list(missing_segments)
        response_data['segments_without_phrases'] = segments_without_phrases
        response_data['all_segments_have_phrases'] = len(segments_without_phrases) == 0
        
        # Corrigir o status se necessário
        if session_data.get('status') == 'completed' and (missing_segments or segments_without_phrases):
            # Se o status é 'completed' mas há segmentos faltando ou sem frases, corrigir para 'transcribing'
            response_data['status'] = 'transcribing'
    
            # Registrar avisos sobre segmentos faltantes ou sem frases
            if missing_segments:
                logger.warning(f"Sessão {session_id} marcada como completa, mas faltam os segmentos {missing_segments}")
            if segments_without_phrases:
                logger.warning(f"Sessão {session_id} marcada como completa, mas os segmentos {segments_without_phrases} não têm frases com timestamps")
    
    return jsonify(response_data)

# Importar o processador de atas
from ata_processor import AtaProcessor

@app.route('/ata_editor')
def ata_editor():
    """Página para edição e geração de atas a partir das transcrições."""
    # Obter todas as sessões disponíveis
    sessions = []
    session_data_dict = {}
    
    try:
        for filename in os.listdir(app.config['DATA_FOLDER']):
            if filename.endswith('.json'):
                try:
                    with open(os.path.join(app.config['DATA_FOLDER'], filename), 'r') as f:
                        session_data = json.load(f)
                        
                        # Garantir que todos os campos necessários existam
                        if 'session_id' not in session_data:
                            session_data['session_id'] = os.path.splitext(filename)[0]
                        
                        if 'title' not in session_data:
                            session_data['title'] = f"Sessão {session_data['session_id'][:8]}"
                        
                        if 'date' not in session_data:
                            session_data['date'] = datetime.now().strftime('%Y-%m-%d')
                        
                        # Verificar se a ata existe e garantir que o campo generated_at seja uma string
                        if 'ata' in session_data and 'generated_at' in session_data['ata']:
                            if not isinstance(session_data['ata']['generated_at'], str):
                                session_data['ata']['generated_at'] = str(session_data['ata']['generated_at'])
                        
                        # Incluir apenas sessões com transcrição completa
                        if 'transcript' in session_data and session_data.get('status') == 'completed':
                            # Adicionar à lista de sessões para o dropdown
                            sessions.append(session_data)
                            # Adicionar ao dicionário para acesso via JavaScript
                            session_id = session_data.get('session_id')
                            if session_id:
                                session_data_dict[session_id] = session_data
                except Exception as e:
                    app.logger.error(f"Erro ao processar o arquivo {filename}: {str(e)}")
                    continue
    except Exception as e:
        app.logger.error(f"Erro ao listar arquivos no diretório de dados: {str(e)}")
    
    # Ordenar sessões por data (mais recentes primeiro)
    sessions.sort(key=lambda x: x.get('date', ''), reverse=True)
    
    return render_template('ata_editor.html', 
                           sessions=sessions, 
                           session_data=session_data_dict)

@app.route('/process_ata', methods=['POST'])
def process_ata():
    """Processa a transcrição e gera uma ata estruturada."""
    session_id = request.form.get('session_id')
    tipo_sessao = request.form.get('tipo_sessao')
    
    if not session_id or not tipo_sessao:
        flash('Sessão e tipo de sessão são obrigatórios')
        return redirect(url_for('ata_editor'))
    
    # Obter dados da sessão
    session_data = get_session_data(session_id)
    if not session_data:
        flash('Sessão não encontrada')
        return redirect(url_for('ata_editor'))
    
    # Obter o conteúdo da ata do formulário
    ata_content = request.form.get('ata_content')
    if not ata_content:
        flash('O conteúdo da ata não pode estar vazio')
        return redirect(url_for('edit_ata', session_id=session_id))
    
    # Coletar metadados do formulário
    metadata = {}
    for key, value in request.form.items():
        if key not in ['session_id', 'tipo_sessao', 'ata_content'] and not key.startswith('cabecalho_') and not key.startswith('corpo_') and key != 'encerramento':
            metadata[key] = value
    
    # Adicionar metadados da sessão
    metadata['title'] = session_data.get('title', '')
    metadata['date'] = session_data.get('date', '')
    
    # Coletar as seções da ata
    sections = {
        'cabecalho': {
            'estrutura': request.form.get('cabecalho_estrutura', ''),
            'presidencia': request.form.get('cabecalho_presidencia', ''),
            'presenca': request.form.get('cabecalho_presenca', '')
        },
        'corpo': {
            'abertura': request.form.get('corpo_abertura', '').split('\n'),
            'expediente': {
                'conteudo': request.form.get('corpo_expediente', ''),
                'marcadores': {
                    'inicio': ['LEITURA DO EXPEDIENTE', 'EXPEDIENTE DO DIA', 'EXPEDIENTE'],
                    'fim': ['NÃO HAVENDO MAIS EXPEDIENTE', 'ENCERRADO O EXPEDIENTE', 'ORDEM DO DIA']
                }
            },
            'pronunciamentos': {
                'conteudo': request.form.get('corpo_pronunciamentos', ''),
                'marcadores': {
                    'inicio': ['FACULTA A PALAVRA AOS SENHORES VEREADORES', 'PALAVRA LIVRE', 'PEQUENO EXPEDIENTE'],
                    'fim': ['ORDEM DO DIA', 'PASSA-SE À ORDEM DO DIA', 'ENCERRADO O PEQUENO EXPEDIENTE']
                }
            },
            'ordem_do_dia': {
                'conteudo': request.form.get('corpo_ordem_do_dia', ''),
                'marcadores': {
                    'inicio': ['ORDEM DO DIA', 'PASSAMOS À ORDEM DO DIA', 'INICIA-SE A ORDEM DO DIA'],
                    'fim': ['ENCERRADA A ORDEM DO DIA', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'NADA MAIS HAVENDO A TRATAR']
                }
            },
            'votacoes': {
                'conteudo': request.form.get('corpo_votacoes', ''),
                'marcadores': {
                    'inicio': ['SUBMETE EM VOTAÇÃO', 'COLOCO EM VOTAÇÃO', 'PASSAMOS À VOTAÇÃO'],
                    'fim': ['APROVADO POR UNANIMIDADE', 'APROVADO POR MAIORIA', 'REJEITADO']
                }
            }
        },
        'encerramento': {
            'texto': request.form.get('encerramento', ''),
            'marcadores': {
                'inicio': ['NADA MAIS HAVENDO A TRATAR', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'COMO NÃO HÁ MAIS NADA A DELIBERAR'],
                'fim': ['ENCERRADA A SESSÃO', 'SESSÃO ENCERRADA', 'ESTÁ ENCERRADA A SESSÃO']
            }
        }
    }
    
    # Salvar a ata processada no arquivo da sessão
    session_data['ata'] = {
        'content': ata_content,
        'tipo_sessao': tipo_sessao,
        'metadata': metadata,
        'sections': sections,
        'generated_at': datetime.now().isoformat()
    }
    
    with open(os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json"), 'w') as f:
        json.dump(session_data, f, indent=2)
    
    # Redirecionar para a visualização da ata
    flash('Ata gerada com sucesso!', 'success')
    return redirect(url_for('view_ata', session_id=session_id))

@app.route('/download_ata/<session_id>')
def download_ata(session_id):
    """Gera e faz download do documento da ata."""
    # Obter dados da sessão
    session_data = get_session_data(session_id)
    if not session_data or 'ata' not in session_data:
        flash('Ata não encontrada')
        return redirect(url_for('ata_editor'))
    
    # Verificar se a estrutura da ata está completa
    if 'content' not in session_data['ata']:
        flash('Conteúdo da ata não encontrado')
        return redirect(url_for('edit_ata', session_id=session_id))
    
    # Inicializar o processador de atas
    templates_dir = os.path.join(app.root_path, 'templates', 'ata_templates')
    ata_processor = AtaProcessor(templates_dir)
    
    # Gerar documento DOCX
    docx_file = ata_processor.generate_docx(
        session_data['ata']['content'],
        session_data['ata']['metadata']
    )
    
    # Enviar o arquivo para download
    return send_file(
        docx_file,
        as_attachment=True,
        download_name=f"ata_{session_id}.docx",
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )

@app.route('/edit_ata/<session_id>')
def edit_ata(session_id):
    """Página para editar uma ata existente."""
    # Obter dados da sessão
    session_data = get_session_data(session_id)
    if not session_data:
        flash('Sessão não encontrada', 'danger')
        return redirect(url_for('ata_editor'))
    
    # Verificar se a sessão tem uma ata
    if 'ata' not in session_data:
        flash('Esta sessão ainda não possui uma ata. Redirecionando para criar uma nova.', 'warning')
        return redirect(url_for('new_ata', session_id=session_id))
    
    # Obter o tipo de sessão da URL ou da ata existente
    tipo_sessao = request.args.get('tipo')
    if not tipo_sessao:
        tipo_sessao = session_data['ata'].get('tipo_sessao', '')
        
    # Carregar todos os templates disponíveis
    templates_dir = os.path.join(app.root_path, 'templates', 'ata_templates')
    config_path = os.path.join(templates_dir, 'config.json')
    
    # Carregar configuração dos templates
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    # Carregar todos os templates disponíveis
    templates = {}
    for tipo, arquivo in config.get('tipos_sessao', {}).items():
        template_path = os.path.join(templates_dir, arquivo)
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                templates[tipo] = json.load(f)
        except Exception as e:
            logger.error(f"Erro ao carregar template {arquivo}: {str(e)}")
    
    # Carregar o template específico para o tipo de sessão atual
    ata_processor = AtaProcessor(templates_dir)
    template = ata_processor._load_template(tipo_sessao)
    
    # Verificar se a ata tem a estrutura de seções (para compatibilidade com atas antigas)
    if 'sections' not in session_data['ata']:
        # Inicializar a estrutura de seções com base no template
        if template:
            session_data['ata']['sections'] = {
                'cabecalho': {
                    'estrutura': f"ATA DA {session_data['ata'].get('numero_sessao', '')}ª REUNIÃO {tipo_sessao.upper()} DA {session_data['ata'].get('numero_sessao_legislativa', '')}ª SESSÃO LEGISLATIVA DA {session_data['ata'].get('numero_legislatura', '')}ª LEGISLATURA DA CÂMARA MUNICIPAL DE {session_data['ata'].get('cidade', '')}, REALIZADA NO DIA {session_data['ata'].get('dia', '')} DE {session_data['ata'].get('mes', '')} DE {session_data['ata'].get('ano', '')}.",
                    'presidencia': f"SOB A PRESIDÊNCIA DOS SENHORES VEREADORES: {session_data['ata'].get('presidente', '')}, PRESIDENTE; {session_data['ata'].get('primeiro_secretario', '')}, 1º SECRETÁRIO; {session_data['ata'].get('segundo_secretario', '')}, 2º SECRETÁRIO.",
                    'presenca': f"ÀS {session_data['ata'].get('hora', '')}H{session_data['ata'].get('minutos', '')}, PRESENTES OS SENHORES VEREADORES: {session_data['ata'].get('vereadores_presentes', '')}. CONSULTADO O LIVRO DE PRESENÇA DOS SENHORES VEREADORES, QUE ACUSA O COMPARECIMENTO DE {session_data['ata'].get('numero_presentes', '')} ({session_data['ata'].get('numero_presentes_extenso', '')}) VEREADORES, FOI ABERTA À SESSÃO."
                },
                'corpo': {
                    'abertura': template.get('corpo', {}).get('abertura', ["O SENHOR PRESIDENTE PROCEDE À CHAMADA NOMINAL DOS SENHORES VEREADORES PRESENTES.", "O SENHOR PRESIDENTE CONVOCA O SENHOR 1º SECRETÁRIO A PROCEDER A LEITURA DO EXPEDIENTE:"]),
                    'expediente': {
                        'conteudo': '',
                        'marcadores': template.get('corpo', {}).get('expediente', {}).get('marcadores', {
                            'inicio': ['LEITURA DO EXPEDIENTE', 'EXPEDIENTE DO DIA', 'EXPEDIENTE', 'PROCEDER A LEITURA DO EXPEDIENTE', 'DETERMINE O SENHOR PRIMEIRO SECRETÁRIO A PROCEDER A LEITURA DO EXPEDIENTE'],
                            'fim': ['NÃO HAVENDO MAIS EXPEDIENTE', 'ENCERRADO O EXPEDIENTE', 'ENCERRADA A LEITURA DO EXPEDIENTE', 'ORDEM DO DIA']
                        })
                    },
                    'pronunciamentos': {
                        'conteudo': '',
                        'marcadores': template.get('corpo', {}).get('pronunciamentos', {}).get('marcadores', {
                            'inicio': ['FACULTA A PALAVRA AOS SENHORES VEREADORES', 'PALAVRA LIVRE', 'PEQUENO EXPEDIENTE'],
                            'fim': ['ORDEM DO DIA', 'PASSA-SE À ORDEM DO DIA', 'ENCERRADO O PEQUENO EXPEDIENTE']
                        })
                    },
                    'ordem_do_dia': {
                        'conteudo': '',
                        'marcadores': template.get('corpo', {}).get('ordem_do_dia', {}).get('marcadores', {
                            'inicio': ['ORDEM DO DIA', 'PASSAMOS À ORDEM DO DIA', 'INICIA-SE A ORDEM DO DIA'],
                            'fim': ['ENCERRADA A ORDEM DO DIA', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'NADA MAIS HAVENDO A TRATAR']
                        })
                    },
                    'votacoes': {
                        'conteudo': '',
                        'marcadores': template.get('corpo', {}).get('votacoes', {}).get('marcadores', {
                            'inicio': ['SUBMETE EM VOTAÇÃO', 'COLOCO EM VOTAÇÃO', 'PASSAMOS À VOTAÇÃO'],
                            'fim': ['APROVADO POR UNANIMIDADE', 'APROVADO POR MAIORIA', 'REJEITADO']
                        })
                    }
                },
                'encerramento': {
                    'texto': f"COMO NÃO HÁ MAIS NADA A DELIBERAR, O SENHOR PRESIDENTE LEVANTA A SESSÃO E CONVOCA UMA OUTRA PARA O DIA {session_data['ata'].get('dia_proxima_sessao', '')} DE {session_data['ata'].get('mes_proxima_sessao', '')} DO CORRENTE ANO NO HORÁRIO REGIMENTAL.",
                    'marcadores': template.get('encerramento', {}).get('marcadores', {
                        'inicio': ['NADA MAIS HAVENDO A TRATAR', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'COMO NÃO HÁ MAIS NADA A DELIBERAR'],
                        'fim': ['ENCERRADA A SESSÃO', 'SESSÃO ENCERRADA', 'ESTÁ ENCERRADA A SESSÃO']
                    })
                }
            }
        else:
            # Fallback para a estrutura padrão se o template não for encontrado
            session_data['ata']['sections'] = {
                'cabecalho': {
                    'estrutura': f"ATA DA {session_data['ata'].get('numero_sessao', '')}ª REUNIÃO {tipo_sessao.upper()} DA {session_data['ata'].get('numero_sessao_legislativa', '')}ª SESSÃO LEGISLATIVA DA {session_data['ata'].get('numero_legislatura', '')}ª LEGISLATURA DA CÂMARA MUNICIPAL DE {session_data['ata'].get('cidade', '')}, REALIZADA NO DIA {session_data['ata'].get('dia', '')} DE {session_data['ata'].get('mes', '')} DE {session_data['ata'].get('ano', '')}.",
                    'presidencia': f"SOB A PRESIDÊNCIA DOS SENHORES VEREADORES: {session_data['ata'].get('presidente', '')}, PRESIDENTE; {session_data['ata'].get('primeiro_secretario', '')}, 1º SECRETÁRIO; {session_data['ata'].get('segundo_secretario', '')}, 2º SECRETÁRIO.",
                    'presenca': f"ÀS {session_data['ata'].get('hora', '')}H{session_data['ata'].get('minutos', '')}, PRESENTES OS SENHORES VEREADORES: {session_data['ata'].get('vereadores_presentes', '')}. CONSULTADO O LIVRO DE PRESENÇA DOS SENHORES VEREADORES, QUE ACUSA O COMPARECIMENTO DE {session_data['ata'].get('numero_presentes', '')} ({session_data['ata'].get('numero_presentes_extenso', '')}) VEREADORES, FOI ABERTA À SESSÃO."
                },
                'corpo': {
                    'abertura': ["O SENHOR PRESIDENTE PROCEDE À CHAMADA NOMINAL DOS SENHORES VEREADORES PRESENTES.", "O SENHOR PRESIDENTE CONVOCA O SENHOR 1º SECRETÁRIO A PROCEDER A LEITURA DO EXPEDIENTE:"],
                    'expediente': {
                        'conteudo': '',
                        'marcadores': {
                            'inicio': ['LEITURA DO EXPEDIENTE', 'EXPEDIENTE DO DIA', 'EXPEDIENTE', 'PROCEDER A LEITURA DO EXPEDIENTE', 'DETERMINE O SENHOR PRIMEIRO SECRETÁRIO A PROCEDER A LEITURA DO EXPEDIENTE'],
                            'fim': ['NÃO HAVENDO MAIS EXPEDIENTE', 'ENCERRADO O EXPEDIENTE', 'ENCERRADA A LEITURA DO EXPEDIENTE', 'ORDEM DO DIA']
                        }
                    },
                    'pronunciamentos': {
                        'conteudo': '',
                        'marcadores': {
                            'inicio': ['FACULTA A PALAVRA AOS SENHORES VEREADORES', 'PALAVRA LIVRE', 'PEQUENO EXPEDIENTE'],
                            'fim': ['ORDEM DO DIA', 'PASSA-SE À ORDEM DO DIA', 'ENCERRADO O PEQUENO EXPEDIENTE']
                        }
                    },
                    'ordem_do_dia': {
                        'conteudo': '',
                        'marcadores': {
                            'inicio': ['ORDEM DO DIA', 'PASSAMOS À ORDEM DO DIA', 'INICIA-SE A ORDEM DO DIA'],
                            'fim': ['ENCERRADA A ORDEM DO DIA', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'NADA MAIS HAVENDO A TRATAR']
                        }
                    },
                    'votacoes': {
                        'conteudo': '',
                        'marcadores': {
                            'inicio': ['SUBMETE EM VOTAÇÃO', 'COLOCO EM VOTAÇÃO', 'PASSAMOS À VOTAÇÃO'],
                            'fim': ['APROVADO POR UNANIMIDADE', 'APROVADO POR MAIORIA', 'REJEITADO']
                        }
                    }
                },
                'encerramento': {
                    'texto': f"COMO NÃO HÁ MAIS NADA A DELIBERAR, O SENHOR PRESIDENTE LEVANTA A SESSÃO E CONVOCA UMA OUTRA PARA O DIA {session_data['ata'].get('dia_proxima_sessao', '')} DE {session_data['ata'].get('mes_proxima_sessao', '')} DO CORRENTE ANO NO HORÁRIO REGIMENTAL.",
                    'marcadores': {
                        'inicio': ['NADA MAIS HAVENDO A TRATAR', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'COMO NÃO HÁ MAIS NADA A DELIBERAR'],
                        'fim': ['ENCERRADA A SESSÃO', 'SESSÃO ENCERRADA', 'ESTÁ ENCERRADA A SESSÃO']
                    }
                }
            }
        
        # Salvar as alterações no arquivo JSON
        with open(os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json"), 'w') as f:
            json.dump(session_data, f, indent=2)
    
    # Preparar dados para o template
    metadata = session_data['ata'].get('metadata', {})
    
    # Se não houver metadata, usar os campos da ata diretamente (para compatibilidade com atas antigas)
    if not metadata and isinstance(session_data['ata'], dict):
        metadata = {k: v for k, v in session_data['ata'].items() if k not in ['content', 'tipo_sessao', 'generated_at', 'sections']}
    
    # Garantir que todos os campos necessários existam no metadata
    campos_padrao = {
        'numero_sessao': '',
        'dia': '',
        'mes': '',
        'ano': '',
        'cidade': '',
        'hora': '',
        'minutos': '',
        'presidente': '',
        'vereadores_presentes': '',
        'numero_presentes': '',
        'numero_presentes_extenso': ''
    }
    
    # Campos específicos para sessão ordinária
    if tipo_sessao == 'ordinaria':
        campos_padrao.update({
            'periodo': '',
            'numero_sessao_legislativa': '',
            'numero_legislatura': '',
            'primeiro_secretario': '',
            'segundo_secretario': '',
            'dia_proxima_sessao': '',
            'mes_proxima_sessao': ''
        })
    
    # Campos específicos para sessão extraordinária
    elif tipo_sessao == 'extraordinaria':
        campos_padrao.update({
            'numero_sessao_legislativa': '',
            'numero_legislatura': '',
            'primeiro_secretario': '',
            'segundo_secretario': '',
            'numero_artigo': ''
        })
    
    # Campos específicos para sessão solene
    elif tipo_sessao == 'solene':
        campos_padrao.update({
            'motivo_sessao': '',
            'autoridades_presentes': ''
        })
    
    # Preencher campos ausentes com valores padrão
    for campo, valor_padrao in campos_padrao.items():
        if campo not in metadata:
            metadata[campo] = valor_padrao
    
    # Extrair os marcadores de todos os templates para uso no frontend
    marcadores = {}
    for tipo, template_data in templates.items():
        marcadores[tipo] = {
            'expediente': {
                'inicio': template_data.get('corpo', {}).get('expediente', {}).get('marcadores', {}).get('inicio', []),
                'fim': template_data.get('corpo', {}).get('expediente', {}).get('marcadores', {}).get('fim', [])
            },
            'pronunciamentos': {
                'inicio': template_data.get('corpo', {}).get('pronunciamentos', {}).get('marcadores', {}).get('inicio', []),
                'fim': template_data.get('corpo', {}).get('pronunciamentos', {}).get('marcadores', {}).get('fim', [])
            },
            'ordem_do_dia': {
                'inicio': template_data.get('corpo', {}).get('ordem_do_dia', {}).get('marcadores', {}).get('inicio', []),
                'fim': template_data.get('corpo', {}).get('ordem_do_dia', {}).get('marcadores', {}).get('fim', [])
            },
            'votacoes': {
                'inicio': template_data.get('corpo', {}).get('votacoes', {}).get('marcadores', {}).get('inicio', []),
                'fim': template_data.get('corpo', {}).get('votacoes', {}).get('marcadores', {}).get('fim', [])
            },
            'encerramento': {
                'inicio': template_data.get('encerramento', {}).get('marcadores', {}).get('inicio', []),
                'fim': template_data.get('encerramento', {}).get('marcadores', {}).get('fim', [])
            }
        }
    
    return render_template('edit_ata.html', 
                           session=session_data, 
                           tipo_sessao=tipo_sessao, 
                           metadata=metadata,
                           marcadores=marcadores,
                           templates=templates,
                           tipos_sessao=list(config.get('tipos_sessao', {}).keys()))

@app.route('/view_ata/<session_id>')
def view_ata(session_id):
    """Página para visualizar uma ata existente."""
    # Obter dados da sessão
    session_data = get_session_data(session_id)
    if not session_data:
        flash('Sessão não encontrada', 'danger')
        return redirect(url_for('ata_editor'))
    
    # Verificar se a ata existe e tem conteúdo
    if 'ata' not in session_data or not isinstance(session_data['ata'], dict) or 'content' not in session_data['ata']:
        flash('Ata não encontrada ou sem conteúdo', 'danger')
        return redirect(url_for('ata_editor'))
    
    # Formatar o conteúdo para exibição HTML
    formatted_content = session_data['ata']['content'].replace('\n\n', '<br><br>').replace('\n', '<br>')
    
    return render_template('view_ata.html', 
                           session=session_data, 
                           ata_content=formatted_content)

@app.route('/new_ata/<session_id>')
def new_ata(session_id):
    """Página para criar uma nova ata."""
    # Obter dados da sessão
    session_data = get_session_data(session_id)
    if not session_data:
        flash('Sessão não encontrada', 'danger')
        return redirect(url_for('ata_editor'))
    
    # Verificar se a transcrição está completa
    if session_data.get('status') != 'completed' or 'transcript' not in session_data:
        flash('A transcrição desta sessão ainda não foi concluída', 'warning')
        return redirect(url_for('ata_editor'))
    
    # Obter o tipo de sessão da URL
    tipo_sessao = request.args.get('tipo', '')
    
    # Extrair informações da data da sessão
    data_sessao = session_data.get('date', '')
    dia, mes, ano = '', '', ''
    
    if data_sessao:
        try:
            # Tentar extrair dia, mês e ano da data
            if '-' in data_sessao:  # Formato ISO: YYYY-MM-DD
                partes = data_sessao.split('-')
                if len(partes) >= 3:
                    ano = partes[0]
                    mes_num = int(partes[1])
                    dia = partes[2].split('T')[0] if 'T' in partes[2] else partes[2]
                    
                    # Converter número do mês para nome
                    meses = ['JANEIRO', 'FEVEREIRO', 'MARÇO', 'ABRIL', 'MAIO', 'JUNHO', 
                             'JULHO', 'AGOSTO', 'SETEMBRO', 'OUTUBRO', 'NOVEMBRO', 'DEZEMBRO']
                    if 1 <= mes_num <= 12:
                        mes = meses[mes_num - 1]
        except Exception as e:
            app.logger.error(f"Erro ao extrair informações da data: {str(e)}")
    
    # Preparar metadata com valores iniciais
    metadata = {
        'numero_sessao': '1',
        'dia': dia,
        'mes': mes,
        'ano': ano,
        'cidade': 'ANGICOS',
        'hora': '18',
        'minutos': '00',
        'presidente': '',
        'vereadores_presentes': '',
        'numero_presentes': '9',
        'numero_presentes_extenso': 'nove'
    }
    
    # Adicionar campos específicos para cada tipo de sessão
    if tipo_sessao == 'ordinaria':
        metadata.update({
            'periodo': '1º PERÍODO ORDINÁRIO',
            'numero_sessao_legislativa': '2',
            'numero_legislatura': '18',
            'primeiro_secretario': '',
            'segundo_secretario': '',
            'dia_proxima_sessao': '',
            'mes_proxima_sessao': ''
        })
    elif tipo_sessao == 'extraordinaria':
        metadata.update({
            'numero_sessao_legislativa': '2',
            'numero_legislatura': '18',
            'primeiro_secretario': '',
            'segundo_secretario': '',
            'numero_artigo': '134'
        })
    elif tipo_sessao == 'solene':
        metadata.update({
            'motivo_sessao': '',
            'autoridades_presentes': ''
        })
    
    # Inicializar a estrutura de seções para a nova ata
    if 'ata' not in session_data:
        session_data['ata'] = {}
    
    # Adicionar a estrutura de seções
    session_data['ata']['sections'] = {
        'cabecalho': {
            'estrutura': '',  # Será preenchido pelo template
            'presidencia': '',
            'presenca': ''
        },
        'corpo': {
            'abertura': ["O SENHOR PRESIDENTE PROCEDE À CHAMADA NOMINAL DOS SENHORES VEREADORES PRESENTES.", "O SENHOR PRESIDENTE CONVOCA O SENHOR 1º SECRETÁRIO A PROCEDER A LEITURA DO EXPEDIENTE:"],
            'expediente': {
                'conteudo': '',
                'marcadores': {
                    'inicio': ['LEITURA DO EXPEDIENTE', 'EXPEDIENTE DO DIA', 'EXPEDIENTE'],
                    'fim': ['NÃO HAVENDO MAIS EXPEDIENTE', 'ENCERRADO O EXPEDIENTE', 'ORDEM DO DIA']
                }
            },
            'pronunciamentos': {
                'conteudo': '',
                'marcadores': {
                    'inicio': ['FACULTA A PALAVRA AOS SENHORES VEREADORES', 'PALAVRA LIVRE', 'PEQUENO EXPEDIENTE'],
                    'fim': ['ORDEM DO DIA', 'PASSA-SE À ORDEM DO DIA', 'ENCERRADO O PEQUENO EXPEDIENTE']
                }
            },
            'ordem_do_dia': {
                'conteudo': '',
                'marcadores': {
                    'inicio': ['ORDEM DO DIA', 'PASSAMOS À ORDEM DO DIA', 'INICIA-SE A ORDEM DO DIA'],
                    'fim': ['ENCERRADA A ORDEM DO DIA', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'NADA MAIS HAVENDO A TRATAR']
                }
            },
            'votacoes': {
                'conteudo': '',
                'marcadores': {
                    'inicio': ['SUBMETE EM VOTAÇÃO', 'COLOCO EM VOTAÇÃO', 'PASSAMOS À VOTAÇÃO'],
                    'fim': ['APROVADO POR UNANIMIDADE', 'APROVADO POR MAIORIA', 'REJEITADO']
                }
            }
        },
        'encerramento': {
            'texto': '',  # Será preenchido pelo template
            'marcadores': {
                'inicio': ['NADA MAIS HAVENDO A TRATAR', 'NÃO HAVENDO MAIS NADA A DELIBERAR', 'COMO NÃO HÁ MAIS NADA A DELIBERAR'],
                'fim': ['ENCERRADA A SESSÃO', 'SESSÃO ENCERRADA', 'ESTÁ ENCERRADA A SESSÃO']
            }
        }
    }
    
    # Extrair conteúdo da transcrição com base nos marcadores
    if 'transcript' in session_data and session_data['transcript']:
        # Função auxiliar para extrair conteúdo entre marcadores
        def extrair_conteudo_entre_marcadores(transcript, marcadores_inicio, marcadores_fim):
            # Converter a transcrição em um único texto para facilitar a busca
            texto_completo = ""
            for segment in transcript:
                if 'text' in segment:
                    texto_completo += segment['text'] + " "
            
            texto_completo = texto_completo.upper()
            
            # Procurar pelo primeiro marcador de início que aparece no texto
            inicio = -1
            for marcador in marcadores_inicio:
                pos = texto_completo.find(marcador)
                if pos != -1 and (inicio == -1 or pos < inicio):
                    inicio = pos + len(marcador)
            
            # Procurar pelo primeiro marcador de fim que aparece depois do início
            fim = len(texto_completo)
            if inicio != -1:
                for marcador in marcadores_fim:
                    pos = texto_completo.find(marcador, inicio)
                    if pos != -1 and pos < fim:
                        fim = pos
            
            # Se encontrou ambos os marcadores, retorna o conteúdo entre eles
            if inicio != -1 and fim < len(texto_completo):
                return texto_completo[inicio:fim].strip()
            
            return ""
        
        # Extrair conteúdo para cada seção com base nos marcadores
        # Expediente
        marcadores_inicio_expediente = session_data['ata']['sections']['corpo']['expediente']['marcadores']['inicio']
        marcadores_fim_expediente = session_data['ata']['sections']['corpo']['expediente']['marcadores']['fim']
        conteudo_expediente = extrair_conteudo_entre_marcadores(session_data['transcript'], marcadores_inicio_expediente, marcadores_fim_expediente)
        if conteudo_expediente:
            session_data['ata']['sections']['corpo']['expediente']['conteudo'] = conteudo_expediente
        
        # Pronunciamentos
        marcadores_inicio_pronunciamentos = session_data['ata']['sections']['corpo']['pronunciamentos']['marcadores']['inicio']
        marcadores_fim_pronunciamentos = session_data['ata']['sections']['corpo']['pronunciamentos']['marcadores']['fim']
        conteudo_pronunciamentos = extrair_conteudo_entre_marcadores(session_data['transcript'], marcadores_inicio_pronunciamentos, marcadores_fim_pronunciamentos)
        if conteudo_pronunciamentos:
            session_data['ata']['sections']['corpo']['pronunciamentos']['conteudo'] = conteudo_pronunciamentos
        
        # Ordem do dia
        marcadores_inicio_ordem = session_data['ata']['sections']['corpo']['ordem_do_dia']['marcadores']['inicio']
        marcadores_fim_ordem = session_data['ata']['sections']['corpo']['ordem_do_dia']['marcadores']['fim']
        conteudo_ordem = extrair_conteudo_entre_marcadores(session_data['transcript'], marcadores_inicio_ordem, marcadores_fim_ordem)
        if conteudo_ordem:
            session_data['ata']['sections']['corpo']['ordem_do_dia']['conteudo'] = conteudo_ordem
        
        # Votações
        marcadores_inicio_votacoes = session_data['ata']['sections']['corpo']['votacoes']['marcadores']['inicio']
        marcadores_fim_votacoes = session_data['ata']['sections']['corpo']['votacoes']['marcadores']['fim']
        conteudo_votacoes = extrair_conteudo_entre_marcadores(session_data['transcript'], marcadores_inicio_votacoes, marcadores_fim_votacoes)
        if conteudo_votacoes:
            session_data['ata']['sections']['corpo']['votacoes']['conteudo'] = conteudo_votacoes
    
    # Salvar as alterações no arquivo JSON
    with open(os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json"), 'w') as f:
        json.dump(session_data, f, indent=2)
    
    # Preparar os metadados da ata para o template
    metadata = {}
    if 'ata' in session_data:
        metadata = {
            'numero_sessao': session_data['ata'].get('numero_sessao', ''),
            'periodo': session_data['ata'].get('periodo', ''),
            'numero_sessao_legislativa': session_data['ata'].get('numero_sessao_legislativa', ''),
            'numero_legislatura': session_data['ata'].get('numero_legislatura', ''),
            'cidade': session_data['ata'].get('cidade', ''),
            'dia': session_data['ata'].get('dia', ''),
            'mes': session_data['ata'].get('mes', ''),
            'ano': session_data['ata'].get('ano', ''),
            'hora': session_data['ata'].get('hora', ''),
            'minutos': session_data['ata'].get('minutos', ''),
            'presidente': session_data['ata'].get('presidente', ''),
            'primeiro_secretario': session_data['ata'].get('primeiro_secretario', ''),
            'segundo_secretario': session_data['ata'].get('segundo_secretario', ''),
            'vereadores_presentes': session_data['ata'].get('vereadores_presentes', ''),
            'numero_presentes': session_data['ata'].get('numero_presentes', ''),
            'numero_presentes_extenso': session_data['ata'].get('numero_presentes_extenso', ''),
            'dia_proxima_sessao': session_data['ata'].get('dia_proxima_sessao', ''),
            'mes_proxima_sessao': session_data['ata'].get('mes_proxima_sessao', ''),
        }
    
    return render_template('edit_ata.html', 
                           session=session_data, 
                           tipo_sessao=tipo_sessao, 
                           metadata=metadata, 
                           is_new=False)

def get_session_data(session_id):
    """Obtém os dados de uma sessão específica pelo ID."""
    try:
        file_path = os.path.join(app.config['DATA_FOLDER'], f"{session_id}.json")
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
        return None
    except Exception as e:
        app.logger.error(f"Erro ao obter dados da sessão {session_id}: {str(e)}")
        return None

@app.route('/get_transcript/<session_id>')
def get_transcript(session_id):
    """Retorna a transcrição de uma sessão em formato JSON para uso via AJAX."""
    session_data = get_session_data(session_id)
    if not session_data or 'transcript' not in session_data:
        app.logger.warning(f"Transcrição não encontrada para a sessão {session_id}")
        return jsonify({
            'success': False,
            'message': 'Transcrição não encontrada'
        })
    
    # Verificar o formato da transcrição para depuração
    transcript = session_data['transcript']
    transcript_type = type(transcript).__name__
    transcript_length = len(transcript) if isinstance(transcript, (list, dict, str)) else 0
    
    app.logger.info(f"Enviando transcrição para a sessão {session_id}. Tipo: {transcript_type}, Tamanho: {transcript_length}")
    
    if isinstance(transcript, list) and transcript_length > 0:
        first_item_type = type(transcript[0]).__name__
        app.logger.info(f"Primeiro item da transcrição é do tipo: {first_item_type}")
        if hasattr(transcript[0], 'keys'):
            app.logger.info(f"Chaves do primeiro item: {list(transcript[0].keys())}")
    
    return jsonify({
        'success': True,
        'transcript': transcript
    })

def get_all_sessions():
    """Retorna todas as sessões disponíveis."""
    sessions = []
    for filename in os.listdir(app.config['DATA_FOLDER']):
        if filename.endswith('.json'):
            with open(os.path.join(app.config['DATA_FOLDER'], filename), 'r') as f:
                session_data = json.load(f)
                sessions.append(session_data)
    return sessions

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
