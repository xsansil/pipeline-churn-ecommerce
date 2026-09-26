"""
Acesso ao PostgreSQL: conexão, criação do banco e registro de execuções.

Todo o pipeline passa por aqui. Concentrar a conexão em um módulo evita que
cada etapa monte a própria URL — e é o que permite trocar host, banco ou
credencial em um lugar só.
"""
import logging
import socket
from datetime import datetime

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from . import config

log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------
# Conexão
# ---------------------------------------------------------------------
def engine(banco=None, criar_se_faltar=False):
    """Devolve um Engine. Com criar_se_faltar, cria o banco caso não exista."""
    if criar_se_faltar:
        garante_banco(banco or config.PG_BANCO)
    eng = create_engine(config.url_banco(banco), pool_pre_ping=True)
    try:
        with eng.connect() as c:
            c.execute(text("select 1"))
    except OperationalError as e:
        raise RuntimeError(
            "Não foi possível conectar em %s\n  %s\n"
            "Verifique se o contêiner do PostgreSQL está no ar e se PG_SENHA "
            "está definida (.env ou variável de ambiente)."
            % (config.url_segura(banco), str(e).splitlines()[0])) from e
    return eng


def garante_banco(nome):
    """CREATE DATABASE precisa rodar fora de transação e em outro banco."""
    adm = create_engine(config.url_banco("postgres"), isolation_level="AUTOCOMMIT")
    with adm.connect() as c:
        existe = c.execute(
            text("select 1 from pg_database where datname = :n"), {"n": nome}
        ).scalar()
        if not existe:
            c.execute(text('create database "%s"' % nome))
            log.info("banco %s criado", nome)
    adm.dispose()


def executa_sql(eng, caminho):
    """Roda um arquivo .sql inteiro. Usado para o DDL das camadas."""
    sql = caminho.read_text(encoding="utf-8")
    with eng.begin() as c:
        c.execute(text(sql))
    log.info("executado %s", caminho.name)


# ---------------------------------------------------------------------
# Escrita e leitura
# ---------------------------------------------------------------------
def grava(df, tabela, schema, eng, modo="replace", indice=False):
    """Grava um DataFrame em <schema>.<tabela> e devolve a quantidade."""
    df.to_sql(tabela, eng, schema=schema, if_exists=modo, index=indice,
              method="multi", chunksize=1000)
    log.info("%s.%s <- %d linhas (%s)", schema, tabela, len(df), modo)
    return len(df)


def le(consulta, eng, **params):
    """Lê uma consulta SQL para DataFrame. Aceita parâmetros nomeados."""
    return pd.read_sql(text(consulta), eng, params=params or None)


def conta(eng, schema, tabela):
    with eng.connect() as c:
        return c.execute(text('select count(*) from %s."%s"' % (schema, tabela))).scalar()


# ---------------------------------------------------------------------
# Proveniência — quem gravou o quê, quando e de onde
# ---------------------------------------------------------------------
_EXECUCAO = {"registros": []}


def inicia_execucao(etapa):
    _EXECUCAO["etapa"] = etapa
    _EXECUCAO["inicio"] = datetime.now()
    _EXECUCAO["registros"] = []


def anota(fase, fonte, quantidade, detalhe=""):
    """Registra um passo do ETL. Vai para o log e para meta.etl_execucoes."""
    _EXECUCAO["registros"].append({
        "momento": datetime.now(),
        "etapa": _EXECUCAO.get("etapa", "?"),
        "fase": fase,
        "fonte": fonte,
        "quantidade": int(quantidade),
        "detalhe": detalhe,
        "host": socket.gethostname(),
    })
    log.info("%-9s %-26s %6d  %s", fase, fonte, quantidade, detalhe)


def grava_execucao(eng):
    """Persiste os passos anotados. Sempre append: é um histórico."""
    if not _EXECUCAO["registros"]:
        return 0
    df = pd.DataFrame(_EXECUCAO["registros"])
    df.to_sql("etl_execucoes", eng, schema="meta", if_exists="append", index=False)
    n = len(df)
    _EXECUCAO["registros"] = []
    return n


# ---------------------------------------------------------------------
# Carga incremental
# ---------------------------------------------------------------------
def ultimo_marco(eng, chave):
    """Último valor processado para uma fonte, ou None na primeira execução."""
    with eng.connect() as c:
        return c.execute(
            text("select valor from meta.controle_extracao where chave = :k"),
            {"k": chave}).scalar()


def marca(eng, chave, valor):
    """Grava o marco da extração. UPSERT: a chave é única por fonte."""
    with eng.begin() as c:
        c.execute(text("""
            insert into meta.controle_extracao (chave, valor, atualizado_em)
            values (:k, :v, now())
            on conflict (chave) do update
               set valor = excluded.valor, atualizado_em = now()
        """), {"k": chave, "v": str(valor)})
