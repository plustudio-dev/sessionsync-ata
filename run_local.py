import os
import sys
import subprocess
import time
import webbrowser
import threading
import signal
import atexit

# Diretórios base
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend')
PREPROCESSING_DIR = os.path.join(BASE_DIR, 'preprocessing')
TRANSCRIPTION_DIR = os.path.join(BASE_DIR, 'transcription')
DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')

# Portas para os serviços (alteradas para evitar conflitos)
FRONTEND_PORT = 8000
PREPROCESSING_PORT = 8001
TRANSCRIPTION_PORT = 8002

# Garantir que os diretórios necessários existem
for dir_path in [DATA_DIR, UPLOADS_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

# Processos
processes = []

def run_service(service_name, directory, port, env_vars=None):
    """Executa um serviço Python."""
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)
    
    # Verificar se o requirements.txt existe e instalar dependências
    req_file = os.path.join(directory, 'requirements.txt')
    if os.path.exists(req_file):
        print(f"Instalando dependências para {service_name}...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file], check=True)
    
    # Iniciar o serviço
    print(f"Iniciando {service_name} na porta {port}...")
    process = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=directory,
        env=env
    )
    processes.append((service_name, process))
    return process

def cleanup():
    """Limpa todos os processos ao encerrar."""
    print("\nEncerrando todos os serviços...")
    for name, process in processes:
        print(f"Encerrando {name}...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"Forçando encerramento de {name}...")
            process.kill()

# Registrar função de limpeza
atexit.register(cleanup)

# Capturar sinais para encerramento limpo
def signal_handler(sig, frame):
    print("Sinal de interrupção recebido.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def main():
    """Função principal para iniciar todos os serviços."""
    try:
        # Iniciar serviço de transcrição
        transcription_env = {
            "FLASK_APP": "app.py",
            "FLASK_ENV": "development",
            "WHISPER_MODEL": "small",  # Usando o modelo small para evitar erros de dimensão de tensor
            "MAX_WORKERS": "2",
            "UPLOAD_FOLDER": UPLOADS_DIR,
            "DATA_FOLDER": DATA_DIR
        }
        transcription_process = run_service(
            "Serviço de Transcrição", 
            TRANSCRIPTION_DIR, 
            TRANSCRIPTION_PORT, 
            transcription_env
        )
        
        # Aguardar um pouco para o serviço de transcrição iniciar
        time.sleep(2)
        
        # Iniciar serviço de pré-processamento
        preprocessing_env = {
            "FLASK_APP": "app.py",
            "FLASK_ENV": "development",
            "TRANSCRIPTION_SERVICE_URL": f"http://localhost:{TRANSCRIPTION_PORT}",
            "UPLOAD_FOLDER": UPLOADS_DIR,
            "DATA_FOLDER": DATA_DIR
        }
        preprocessing_process = run_service(
            "Serviço de Pré-processamento", 
            PREPROCESSING_DIR, 
            PREPROCESSING_PORT, 
            preprocessing_env
        )
        
        # Aguardar um pouco para o serviço de pré-processamento iniciar
        time.sleep(2)
        
        # Iniciar frontend
        frontend_env = {
            "FLASK_APP": "app.py",
            "FLASK_ENV": "development",
            "PREPROCESSING_SERVICE_URL": f"http://localhost:{PREPROCESSING_PORT}",
            "TRANSCRIPTION_SERVICE_URL": f"http://localhost:{TRANSCRIPTION_PORT}",
            "UPLOAD_FOLDER": UPLOADS_DIR,
            "DATA_FOLDER": DATA_DIR
        }
        frontend_process = run_service(
            "Frontend", 
            FRONTEND_DIR, 
            FRONTEND_PORT, 
            frontend_env
        )
        
        # Abrir o navegador após um breve delay
        def open_browser():
            time.sleep(3)
            print(f"Abrindo navegador em http://localhost:{FRONTEND_PORT}")
            webbrowser.open(f"http://localhost:{FRONTEND_PORT}")
        
        threading.Thread(target=open_browser).start()
        
        print("\nTodos os serviços iniciados!")
        print(f"Frontend: http://localhost:{FRONTEND_PORT}")
        print(f"Serviço de Pré-processamento: http://localhost:{PREPROCESSING_PORT}")
        print(f"Serviço de Transcrição: http://localhost:{TRANSCRIPTION_PORT}")
        print("\nPressione Ctrl+C para encerrar todos os serviços.")
        
        # Manter o script em execução
        while True:
            # Verificar se algum processo terminou
            for name, process in processes:
                if process.poll() is not None:
                    print(f"AVISO: {name} encerrou com código {process.returncode}")
            
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("Interrompido pelo usuário.")
    except Exception as e:
        print(f"Erro: {e}")
    finally:
        cleanup()

if __name__ == "__main__":
    main()
