"""
Etapa 1 — coleta das quatro fontes e carga na camada bronze.

A bronze guarda o dado **como ele chegou**: tudo como texto, sem conversão
e sem descarte. Nada é corrigido aqui. Duas razões:

  * permite reprocessar a limpeza inteira sem voltar às fontes, que podem
    estar fora do ar ou já ter mudado;
  * preserva a evidência. Depois da limpeza, ninguém consegue provar que
    havia 185 duplicatas no arquivo original — a menos que o original
    esteja guardado.

Cada tabela carrega `_ingerido_em` e `_fonte`: quando entrou e de onde
veio. É a proveniência no nível da linha.
"""
import logging

import pandas as pd

from .. import config, db
from ..coleta import catalogo, crm, ibge, transacoes

log = logging.getLogger("pipeline")


def executar(eng, recriar=False):
    db.inicia_execucao("bronze")
    modo = "substituir" if recriar else "acrescentar"
    resumo = {}

    # -----------------------------------------------------------------
    # Fonte 1 — CSV das transações (e o JSON do CRM, gerado junto)
    # -----------------------------------------------------------------
    info = transacoes.gerar()
    resumo["geracao"] = info
    db.anota("COLETA", "1 CSV transacoes", info["linhas_no_csv"],
             "%d clientes, %d duplicatas injetadas"
             % (info["clientes"], info["linhas_no_csv"] - info["transacoes_limpas"]))

    df_tx = pd.read_csv(info["csv"], dtype=str, keep_default_na=False)
    df_tx["_fonte"] = "csv"
    db.grava(df_tx, "transacoes", "bronze", eng, modo)
    resumo["transacoes"] = len(df_tx)

    # -----------------------------------------------------------------
    # Fonte 2 — JSON do catálogo
    # -----------------------------------------------------------------
    caminho, n = catalogo.gerar()
    df_cat = pd.read_json(caminho, dtype=str)
    df_cat["_fonte"] = "json"
    db.grava(df_cat, "catalogo", "bronze", eng, "substituir")  # catálogo é snapshot
    db.anota("COLETA", "2 JSON catalogo", n, caminho.name)
    resumo["catalogo"] = n

    # -----------------------------------------------------------------
    # Fonte 3 — API REST do IBGE (externa; pode falhar)
    # -----------------------------------------------------------------
    municipios, origem = ibge.coletar()
    df_ibge = pd.DataFrame(municipios).drop(columns=["municipio_chave"],
                                            errors="ignore").astype(str)
    df_ibge["_fonte"] = "api_ibge"
    db.grava(df_ibge, "municipios_ibge", "bronze", eng, "substituir")
    db.anota("COLETA", "3 API IBGE", len(df_ibge),
             "UFs %s · origem: %s" % ("/".join(config.IBGE_UFS), origem))
    resumo["ibge"] = len(df_ibge)
    resumo["ibge_origem"] = origem

    # -----------------------------------------------------------------
    # Fonte 4 — PostgreSQL do CRM (carga incremental pela data de cadastro)
    # -----------------------------------------------------------------
    _, total_crm, semeou = crm.semear()
    if semeou:
        db.anota("SETUP", "4 PG crm_origem", total_crm, "banco de origem semeado")

    marco = None if recriar else db.ultimo_marco(eng, "crm.clientes")
    df_crm = crm.extrair(desde=marco)
    if len(df_crm):
        df_crm = df_crm.astype(str)
        df_crm["_fonte"] = "postgresql"
        db.grava(df_crm, "clientes", "bronze", eng,
                 "substituir" if recriar else modo)
        db.marca(eng, "crm.clientes", df_crm["cadastrado_em"].max())
    db.anota("COLETA", "4 PG clientes", len(df_crm),
             "carga completa" if not marco else "incremental desde " + str(marco))
    resumo["clientes"] = len(df_crm)

    # -----------------------------------------------------------------
    for tabela in ("transacoes", "catalogo", "municipios_ibge", "clientes"):
        db.anota("BRONZE", "bronze." + tabela, db.conta(eng, "bronze", tabela),
                 "total na camada")

    db.grava_execucao(eng)
    return resumo
