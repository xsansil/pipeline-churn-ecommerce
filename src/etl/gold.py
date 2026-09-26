"""
Etapa 3 — silver para gold: uma linha por cliente, pronta para o modelo.

A tabela `gold.features_clientes` é a única do projeto cuja estrutura não
está declarada no DDL, e isso é deliberado: o conjunto de colunas depende
das categorias presentes no dado. Se amanhã surgir uma mesorregião nova, o
One-Hot cria mais uma coluna, e um DDL fixo passaria a divergir do que o
pipeline produz.

A tabela é derivada e inteiramente reproduzível, então recriá-la a cada
execução não perde nada. O que se perde com o `to_sql(replace)` do pandas
— chave primária e comentários — é reposto logo em seguida, para que a
tabela continue tendo as garantias das demais.

O dicionário de features vai junto, em `gold.features_dicionario`: sem ele
ninguém sabe o que `razao_recencia_intervalo` significa daqui a seis meses.
"""
import logging

import pandas as pd
from sqlalchemy import text

from .. import config, db, features

log = logging.getLogger("pipeline")

TABELA = "features_clientes"


def _tipo_sql(serie):
    if pd.api.types.is_integer_dtype(serie):
        return "BIGINT"
    if pd.api.types.is_float_dtype(serie):
        return "DOUBLE PRECISION"
    if pd.api.types.is_bool_dtype(serie):
        return "BOOLEAN"
    if pd.api.types.is_datetime64_any_dtype(serie):
        return "TIMESTAMP"
    return "TEXT"


def _cria_tabela(eng, df):
    """CREATE TABLE derivado do DataFrame, com chave primária de verdade."""
    colunas = []
    for nome, serie in df.items():
        nulo = "" if nome == "cliente_id" else ""
        colunas.append('    "%s" %s%s' % (nome, _tipo_sql(serie), nulo))
    ddl = (
        "DROP TABLE IF EXISTS gold.%s;\n"
        "CREATE TABLE gold.%s (\n%s\n);\n"
        "ALTER TABLE gold.%s ADD PRIMARY KEY (cliente_id);\n"
        "COMMENT ON TABLE gold.%s IS "
        "'Uma linha por cliente: features da janela de observacao e o alvo';"
        % (TABELA, TABELA, ",\n".join(colunas), TABELA, TABELA))
    with eng.begin() as c:
        c.execute(text(ddl))


def executar(eng, corte=None):
    db.inicia_execucao("gold")

    tx = db.le("select * from silver.transacoes", eng)
    dim = db.le("select * from silver.clientes", eng)
    db.anota("LEITURA", "silver.transacoes", len(tx), "%d clientes"
             % tx["cliente_id"].nunique())

    # -----------------------------------------------------------------
    # Separação temporal — é o que impede o vazamento
    # -----------------------------------------------------------------
    obs, ava, corte = features.separar_janelas(tx, corte)
    db.anota("JANELA", "observacao (features)", len(obs),
             "%s a %s" % (obs["data"].min().date(), obs["data"].max().date()))
    db.anota("JANELA", "avaliacao (alvo)", len(ava),
             "%s a %s" % (ava["data"].min().date(), ava["data"].max().date()))

    if obs.empty or ava.empty:
        raise RuntimeError(
            "Corte em %s deixou uma das janelas vazia. As features precisam de "
            "histórico antes e o alvo precisa de observação depois." % corte.date())

    # -----------------------------------------------------------------
    # Features e alvo
    # -----------------------------------------------------------------
    f = features.construir(obs, dim=dim, corte=corte)
    f = features.marcar_alvo(f, ava)
    enc = features.codificar(f)

    lista = features.lista_features(enc)
    num = features.numericas(lista)
    db.anota("FEATURES", "construidas", len(lista),
             "%d numericas + %d binarias" % (len(num), len(lista) - len(num)))
    db.anota("FEATURES", "alvo churn", int(enc["churn"].sum()),
             "%.1f%% de %d clientes" % (100 * enc["churn"].mean(), len(enc)))

    novas = [c for c in ("margem_total", "margem_media", "margem_pct",
                         "dias_desde_cadastro", "cliente_cadastrado")
             if c in lista] + [c for c in lista if c.startswith("meso_")]
    db.anota("FEATURES", "vindas da integracao", len(novas),
             "nao existiriam com fonte unica: " + ", ".join(novas[:5]))

    # -----------------------------------------------------------------
    # Carga
    # -----------------------------------------------------------------
    saida = enc.copy()
    for coluna in ("primeira_compra", "ultima_compra"):
        saida[coluna] = pd.to_datetime(saida[coluna]).dt.date
    _cria_tabela(eng, saida)
    db.grava(saida, TABELA, "gold", eng, "acrescentar")

    # -----------------------------------------------------------------
    # Dicionário — o que cada coluna significa, e o quanto se move com o alvo
    # -----------------------------------------------------------------
    dicionario = pd.DataFrame({
        "feature": lista,
        "tipo": ["binaria (One-Hot)" if c.startswith(("meso_", "canal_", "pgto_"))
                 else "binaria" if c == "cliente_cadastrado"
                 else "numerica" for c in lista],
        "origem": [_origem(c) for c in lista],
        "media": [round(float(enc[c].mean()), 4) for c in lista],
        "desvio": [round(float(enc[c].std()), 4) for c in lista],
        "corr_com_churn": [round(float(enc[c].corr(enc["churn"])), 4)
                           for c in lista],
    })
    dicionario.to_sql("features_dicionario", eng, schema="gold",
                      if_exists="replace", index=False)
    db.anota("FEATURES", "gold.features_dicionario", len(dicionario),
             "tipo, origem, media, desvio e correlacao de cada feature")

    db.anota("GOLD", "gold." + TABELA, db.conta(eng, "gold", TABELA),
             "%d features + alvo" % len(lista))

    db.grava_execucao(eng)
    return {"clientes": len(enc), "features": len(lista),
            "churn": float(enc["churn"].mean()), "corte": corte.date()}


def _origem(coluna):
    """De qual fonte a feature depende — usado no dicionário da entrega."""
    if coluna.startswith("meso_"):
        return "API IBGE"
    if coluna.startswith(("margem", "custo")):
        return "catalogo JSON"
    if coluna in ("dias_desde_cadastro",):
        return "CRM PostgreSQL"
    if coluna == "cliente_cadastrado":
        return "juncao CSV x CRM"
    return "extrato CSV"
