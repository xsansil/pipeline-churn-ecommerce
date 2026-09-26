"""
Etapa 2 — bronze para silver: limpeza e integração das fontes.

Aqui as quatro fontes viram uma tabela só. Três junções acontecem, e cada
uma delas recebe um contador explícito:

    cadastro do CRM   -> nome, e-mail, idade e cidade autoritativos
    catálogo (JSON)   -> custo e margem
    IBGE (API)        -> mesorregião do município

O contador não é zelo excessivo. `merge(how="left")` nunca levanta erro:
quando a chave não casa, ele preenche com nulo e segue. Um join que
deveria casar 100% e casa 3% produz uma tabela do tamanho certo, com as
colunas certas, cheia de nulos — e o problema só aparece lá na frente,
como um modelo que não aprende. A contagem é o que transforma isso em uma
linha no log.

Ordem das operações: limpar, enriquecer, e só então imputar. O CRM tem o
dado real do cliente; recorrer à mediana antes de consultá-lo seria
inventar o que já estava disponível.
"""
import logging

import pandas as pd

from .. import db, limpeza

log = logging.getLogger("pipeline")

COLUNAS_SILVER = [
    "transacao_id", "cliente_id", "nome_cliente", "email", "email_valido",
    "idade", "cidade", "estado", "mesorregiao", "data", "produto", "categoria",
    "quantidade", "valor", "preco_unitario", "custo", "margem",
    "canal", "forma_pagamento", "cliente_cadastrado",
]


def executar(eng):
    db.inicia_execucao("silver")

    bruto = db.le("select * from bronze.transacoes", eng)
    db.anota("LEITURA", "bronze.transacoes", len(bruto), "dado cru")

    # -----------------------------------------------------------------
    # Limpeza
    # -----------------------------------------------------------------
    funil = []
    df = limpeza.limpar(bruto, relatorio=funil)
    for rotulo, antes, depois, removidos in funil:
        if removidos:
            db.anota("LIMPEZA", rotulo, removidos, "%d -> %d" % (antes, depois))
    db.anota("LIMPEZA", "total apos limpeza", len(df),
             "%.1f%% descartado" % (100 * (len(bruto) - len(df)) / len(bruto)))

    # -----------------------------------------------------------------
    # Junção 1 — cadastro do CRM (autoritativo)
    # -----------------------------------------------------------------
    crm = db.le("select * from bronze.clientes", eng)
    crm["cliente_id"] = pd.to_numeric(crm["cliente_id"])
    crm["idade_crm"] = pd.to_numeric(crm["idade"])
    crm = crm[["cliente_id", "nome", "email", "idade_crm", "cidade", "estado"]]
    crm.columns = ["cliente_id", "nome_crm", "email_crm", "idade_crm",
                   "cidade_crm", "estado_crm"]

    antes = len(df)
    df = df.merge(crm, on="cliente_id", how="left")
    assert len(df) == antes, "o merge com o CRM duplicou linhas"

    com_cadastro = df["nome_crm"].notna()
    df["cliente_cadastrado"] = com_cadastro
    sem = int((~com_cadastro).sum())
    db.anota("JUNCAO", "1 cadastro do CRM", int(com_cadastro.sum()),
             "%d transacoes de %d clientes sem cadastro mantidas com o dado do CSV"
             % (sem, df.loc[~com_cadastro, "cliente_id"].nunique()))

    # o CRM vence onde existe; o CSV limpo cobre o resto
    cidade_antes_vazia = int((df["cidade"] == "").sum())
    for destino, origem in (("nome_cliente", "nome_crm"), ("email", "email_crm"),
                            ("idade", "idade_crm"), ("cidade", "cidade_crm"),
                            ("estado", "estado_crm")):
        df[destino] = df[origem].where(df[origem].notna(), df[destino])
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    df["email_valido"] = df["email"].str.contains(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$", regex=True, na=False)

    recuperadas = cidade_antes_vazia - int((df["cidade"] == "").sum())
    db.anota("JUNCAO", "cidades recuperadas", recuperadas,
             "estavam vazias no extrato e vieram do cadastro")

    # -----------------------------------------------------------------
    # Junção 2 — catálogo: custo e margem
    # -----------------------------------------------------------------
    cat = db.le("select produto, custo from bronze.catalogo", eng)
    cat["chave"] = cat["produto"].map(limpeza.sem_acento)
    cat["custo_unitario"] = pd.to_numeric(cat["custo"])
    cat = cat[["chave", "custo_unitario"]]

    df["chave_produto"] = df["produto"].map(limpeza.sem_acento)
    antes = len(df)
    df = df.merge(cat, left_on="chave_produto", right_on="chave", how="left")
    assert len(df) == antes, "o merge com o catálogo duplicou linhas"

    sem_custo = int(df["custo_unitario"].isna().sum())
    db.anota("JUNCAO", "2 catalogo (custo)", int(df["custo_unitario"].notna().sum()),
             "sem correspondencia: %d" % sem_custo)
    if sem_custo:
        faltando = sorted(df.loc[df["custo_unitario"].isna(), "produto"].unique())
        raise RuntimeError(
            "Produtos fora do catálogo: %s. O catálogo precisa cobrir tudo o que "
            "foi vendido, senão a margem fica incorreta em silêncio." % faltando)

    df["custo"] = (df["custo_unitario"] * df["quantidade"]).round(2)
    df["margem"] = (df["valor"] - df["custo"]).round(2)

    # -----------------------------------------------------------------
    # Junção 3 — IBGE: mesorregião
    # -----------------------------------------------------------------
    ibge = db.le("select municipio, uf, mesorregiao from bronze.municipios_ibge", eng)
    ibge["chave"] = ibge["municipio"].map(limpeza.sem_acento)
    ibge = ibge.drop_duplicates(subset=["chave", "uf"])[["chave", "uf", "mesorregiao"]]

    df["chave_cidade"] = df["cidade"].map(limpeza.sem_acento)
    antes = len(df)
    df = df.merge(ibge, left_on=["chave_cidade", "estado"], right_on=["chave", "uf"],
                  how="left", suffixes=("", "_ibge"))
    assert len(df) == antes, "o merge com o IBGE duplicou linhas"

    casou = int(df["mesorregiao"].notna().sum())
    db.anota("JUNCAO", "3 IBGE (mesorregiao)", casou,
             "%.1f%% das transacoes; sem correspondencia: %d"
             % (100 * casou / len(df), len(df) - casou))

    nao_casou = sorted(df.loc[df["mesorregiao"].isna(), "cidade"].unique())
    if nao_casou:
        db.anota("JUNCAO", "cidades sem mesorregiao", len(nao_casou),
                 ", ".join(nao_casou[:6]))

    # -----------------------------------------------------------------
    # Imputação — só agora, depois de o CRM ter tido a chance
    # -----------------------------------------------------------------
    imputacoes = []
    df = limpeza.imputar(df, relatorio=imputacoes)
    for rotulo, qtd in imputacoes:
        db.anota("IMPUTACAO", rotulo, qtd, "")

    # -----------------------------------------------------------------
    # Carga
    # -----------------------------------------------------------------
    saida = df[COLUNAS_SILVER].copy()
    saida["data"] = pd.to_datetime(saida["data"]).dt.date
    db.grava(saida, "transacoes", "silver", eng, "substituir")

    db.anota("SILVER", "silver.transacoes", db.conta(eng, "silver", "transacoes"),
             "clientes distintos: %d" % saida["cliente_id"].nunique())

    db.grava_execucao(eng)
    return {"linhas": len(saida), "clientes": saida["cliente_id"].nunique(),
            "sem_cadastro": sem, "cidades_recuperadas": recuperadas}
