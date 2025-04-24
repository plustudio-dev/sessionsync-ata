import os
import shutil
import sys

def clean_directories():
    """Limpa os diretórios de dados e uploads para iniciar do zero."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, 'data')
    uploads_dir = os.path.join(base_dir, 'uploads')
    
    print("Limpando sistema Session Sync para testes...")
    
    # Limpar diretório de dados
    if os.path.exists(data_dir):
        print(f"Removendo conteúdo de {data_dir}...")
        for item in os.listdir(data_dir):
            item_path = os.path.join(data_dir, item)
            try:
                if os.path.isfile(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                print(f"Erro ao remover {item_path}: {e}")
    else:
        print(f"Criando diretório {data_dir}...")
        os.makedirs(data_dir)
    
    # Limpar diretório de uploads
    if os.path.exists(uploads_dir):
        print(f"Removendo conteúdo de {uploads_dir}...")
        for item in os.listdir(uploads_dir):
            item_path = os.path.join(uploads_dir, item)
            try:
                if os.path.isfile(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                print(f"Erro ao remover {item_path}: {e}")
    else:
        print(f"Criando diretório {uploads_dir}...")
        os.makedirs(uploads_dir)
    
    print("Limpeza concluída! O sistema está pronto para novos testes.")
    print("Execute 'python run_local.py' para iniciar o sistema.")

if __name__ == "__main__":
    # Confirmar com o usuário
    if len(sys.argv) > 1 and sys.argv[1] == "--force":
        clean_directories()
    else:
        confirm = input("Esta ação irá remover TODOS os dados de transcrição e uploads. Continuar? (s/n): ")
        if confirm.lower() in ['s', 'sim', 'y', 'yes']:
            clean_directories()
        else:
            print("Operação cancelada.")
