"""
Fonte 4 (PostgreSQL) — o cadastro de clientes do CRM.

Em produção este banco pertence a outro sistema e o pipeline só o lê. Para
que o projeto seja reproduzível em qualquer máquina, o módulo também sabe
criá-lo e populá-lo a partir do JSON gerado na coleta — mas essa semeadura
é setup de ambiente, não parte do fluxo de dados.

A separação importa: o CRM é a **fonte autoritativa** dos atributos do
cliente (nome, e-mail, idade, cidade). O extrato CSV repete esses campos,
mas sujos, porque é um dump operacional. Quando os dois discordam, a
camada silver fica com o CRM.
"""
import json
import logging

import pandas as pd
from sqlalchemy import create_engine, text

from .. import config, db

log = logging.getLogger("pipeline")

BANCO_ORIGEM = "crm_origem"
TABELA = "clientes"

DDL = """
CREATE TABLE IF NOT EXISTS clientes (
    cliente_id     INTEGER PRIMARY KEY,
    nome           TEXT      NOT NULL,
    email          TEXT      NOT NULL,
    idade          SMALLINT  NOT NULL CHECK (idade BETWEEN 18 AND 120),
    cidade         TEXT      NOT NULL,
    estado         CHAR(2)   NOT NULL,
    cadastrado_em  DATE      NOT NULL
);
COMMENT ON TABLE clientes IS
    'Cadastro do CRM — fonte autoritativa dos atributos do cliente';
"""


def engine_origem():
    return create_engine(config.url_banco(BANCO_ORIGEM), pool_pre_ping=True)


def semear(caminho_json=None):
    """
    Cria o banco do CRM e carrega o cadastro. Idempotente.

    Só roda quando a tabela não existe ou está vazia — para não sobrescrever
    um CRM que já esteja em uso.
    """
    caminho = caminho_json or (config.DADOS / "clientes_crm.json")
    registros = json.loads(caminho.read_text(encoding="utf-8"))

    db.garante_banco(BANCO_ORIGEM)
    eng = engine_origem()

    with eng.begin() as c:
        c.execute(text(DDL))
        ja_tem = c.execute(text("select count(*) from clientes")).scalar()

    if ja_tem:
        log.info("CRM já populado (%d cadastros); semeadura pulada", ja_tem)
        return eng, ja_tem, False

    pd.DataFrame(registros).to_sql(TABELA, eng, if_exists="append", index=False,
                                   method="multi", chunksize=500)
    log.info("CRM semeado com %d cadastros", len(registros))
    return eng, len(registros), True


def extrair(eng=None, desde=None):
    """
    Lê o cadastro do CRM. Com `desde`, traz só quem foi cadastrado depois —
    é o gancho da carga incremental, usando a data de cadastro como marco.
    """
    eng = eng or engine_origem()
    if desde:
        sql = "select * from clientes where cadastrado_em > :d order by cliente_id"
        return pd.read_sql(text(sql), eng, params={"d": desde})
    return pd.read_sql(text("select * from clientes order by cliente_id"), eng)
