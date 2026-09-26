"""
Configuração do projeto. As credenciais vêm do ambiente, nunca do código.

A ordem de precedência é: variável de ambiente já definida > arquivo .env >
valor padrão. O .env está no .gitignore; o .env.example, versionado, mostra
quais chaves existem sem revelar nenhum valor.

O parser do .env é próprio, de propósito. As implementações mais comuns tratam
o `#` como início de comentário em qualquer posição, e uma senha contendo `#`
sem aspas chega truncada ao banco. O sintoma é um erro de autenticação que não
diz nada sobre o motivo. Aqui o `#` só inicia comentário quando abre a linha.
"""
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
MODELOS = RAIZ / "modelos"
SQL = RAIZ / "sql"
LOGS = RAIZ / "logs"

for _p in (DADOS, MODELOS, LOGS):
    _p.mkdir(exist_ok=True)


def _carrega_env(caminho=RAIZ / ".env"):
    """Lê o .env para dentro de os.environ, sem sobrescrever o que já existe."""
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave, valor = chave.strip(), valor.strip()
        # aspas envolvendo o valor são delimitadores, não parte da senha
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        os.environ.setdefault(chave, valor)


_carrega_env()


def env(chave, padrao=None, obrigatorio=False):
    valor = os.environ.get(chave, padrao)
    if obrigatorio and not valor:
        raise RuntimeError(
            "Variável %s não definida. Copie .env.example para .env e preencha, "
            "ou exporte a variável no ambiente." % chave)
    return valor


# ---------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------
PG_HOST = env("PG_HOST", "192.168.1.10")
PG_PORT = env("PG_PORT", "5432")
PG_USER = env("PG_USER", "postgres")
PG_BANCO = env("PG_BANCO", "churn_dw")


def url_banco(banco=None):
    """URL do SQLAlchemy. A senha é lida na hora, e nunca fica em atributo."""
    senha = env("PG_SENHA") or env("PGPASSWORD", obrigatorio=True)
    from urllib.parse import quote_plus
    return "postgresql+psycopg2://%s:%s@%s:%s/%s" % (
        PG_USER, quote_plus(senha), PG_HOST, PG_PORT, banco or PG_BANCO)


def url_segura(banco=None):
    """A mesma URL com a senha mascarada, para log e mensagem de erro."""
    return "postgresql+psycopg2://%s:***@%s:%s/%s" % (
        PG_USER, PG_HOST, PG_PORT, banco or PG_BANCO)


# ---------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------
SEMENTE = 42                 # reprodutibilidade do treino
SEMENTE_DADOS = 2026         # reprodutibilidade da geração das transações
CORTE = "2024-10-01"         # fim da observação / início da avaliação
TETO_PRECO_UNITARIO = 1500   # o item mais caro do catálogo custa R$ 900
N_CLIENTES = 400

IBGE_UFS = ["AP", "PA", "AM", "RR"]
IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{}/municipios"

CAMADAS = ["bronze", "silver", "gold", "meta"]
