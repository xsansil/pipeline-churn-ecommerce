"""
Engenharia de features (silver para gold).

Muda a granularidade: de uma linha por transação para uma linha por
cliente. O modelo não classifica compras, classifica pessoas.

## A separação temporal

A definição intuitiva de churn ("está há mais de 90 dias sem comprar")
tem um defeito fatal se `dias sem comprar` também virar feature: a
resposta fica dentro da pergunta. O modelo acerta quase tudo sem ter
aprendido nada, e o resultado só desmorona em produção.

Aqui o tempo é cortado em duas janelas:

    observação   ..< CORTE    constrói as features
    avaliação    >= CORTE     define o alvo

A pergunta usa dados de antes; a resposta vem de depois. O modelo nunca vê
o futuro.

## O que a integração acrescentou

Quatro features saem de fontes que o extrato de vendas não tem, e não
existiriam num pipeline de fonte única:

    margem_total, margem_media   do catálogo JSON
    dias_desde_cadastro          do cadastro no PostgreSQL
    cliente_cadastrado           da própria junção
    mesorregiao_*                da API do IBGE
"""
import logging

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger("pipeline")

CATEGORICAS = ["mesorregiao", "canal_principal", "pagamento_principal"]
DESCARTAR = ["cliente_id", "primeira_compra", "ultima_compra", "churn"]


def separar_janelas(tx, corte=None):
    """Divide as transações nas duas janelas. Devolve (observação, avaliação)."""
    corte = pd.Timestamp(corte or config.CORTE)
    tx = tx.copy()
    tx["data"] = pd.to_datetime(tx["data"])
    return tx[tx["data"] < corte], tx[tx["data"] >= corte], corte


def construir(obs, dim=None, corte=None):
    """
    Agrega as transações da janela de observação em uma linha por cliente.

    `obs` só pode conter transações ANTERIORES ao corte. Qualquer coisa a
    partir dele pertence à janela de avaliação e vaza a resposta.
    `dim` é a dimensão de clientes (silver.clientes), opcional.
    """
    corte = pd.Timestamp(corte or config.CORTE)
    obs = obs.copy()
    obs["data"] = pd.to_datetime(obs["data"])

    # -- agregações do histórico -------------------------------------
    f = obs.groupby("cliente_id").agg(
        total_gasto=("valor", "sum"),
        qtd_compras=("valor", "count"),
        ticket_medio=("valor", "mean"),
        ticket_max=("valor", "max"),
        ticket_std=("valor", "std"),
        itens_totais=("quantidade", "sum"),
        primeira_compra=("data", "min"),
        ultima_compra=("data", "max"),
        produtos_distintos=("produto", "nunique"),
        categorias_distintas=("categoria", "nunique"),
        idade=("idade", "first"),
        margem_total=("margem", "sum"),
        margem_media=("margem", "mean"),
    ).reset_index()

    f["ticket_std"] = f["ticket_std"].fillna(0)      # cliente com uma compra só
    f["itens_por_compra"] = (f["itens_totais"] / f["qtd_compras"]).round(2)
    f["margem_pct"] = (100 * f["margem_total"] / f["total_gasto"]).round(2)

    # -- atributos temporais -----------------------------------------
    f["recencia_dias"] = (corte - f["ultima_compra"]).dt.days
    f["tempo_vida_dias"] = (f["ultima_compra"] - f["primeira_compra"]).dt.days
    f["dias_desde_primeira"] = (corte - f["primeira_compra"]).dt.days

    meses = obs.groupby("cliente_id")["data"].apply(
        lambda s: s.dt.to_period("M").nunique())
    f["meses_ativos"] = f["cliente_id"].map(meses)

    def intervalos(s):
        d = s.sort_values().diff().dt.days.dropna()
        return pd.Series({"intervalo_medio": d.mean(), "intervalo_std": d.std()})

    iv = obs.groupby("cliente_id")["data"].apply(intervalos).unstack()
    f = f.merge(iv, left_on="cliente_id", right_index=True, how="left")
    f["intervalo_medio"] = f["intervalo_medio"].fillna(f["dias_desde_primeira"])
    f["intervalo_std"] = f["intervalo_std"].fillna(0)

    # -- janelas móveis e tendência ----------------------------------
    for dias in (30, 90, 180):
        janela = obs[obs["data"] >= corte - pd.Timedelta(days=dias)]
        f["compras_%dd" % dias] = f["cliente_id"].map(
            janela.groupby("cliente_id").size()).fillna(0)
        f["gasto_%dd" % dias] = f["cliente_id"].map(
            janela.groupby("cliente_id")["valor"].sum()).fillna(0)

    anterior = obs[(obs["data"] >= corte - pd.Timedelta(days=180)) &
                   (obs["data"] < corte - pd.Timedelta(days=90))]
    f["compras_90_180d"] = f["cliente_id"].map(
        anterior.groupby("cliente_id").size()).fillna(0)
    f["tendencia_90d"] = f["compras_90d"] - f["compras_90_180d"]

    # -- quantos ciclos de compra o cliente está atrasado -------------
    # A recência isolada não distingue dois casos opostos. Quem compra a
    # cada 15 dias e está há 90 sem comprar está seis ciclos fora do padrão
    # DELE; quem compra a cada 120 e está há 90 está dentro do normal. A
    # recência é a mesma nos dois, 90 dias.
    f["razao_recencia_intervalo"] = (
        f["recencia_dias"] / f["intervalo_medio"].clip(lower=1)).round(2)

    # -- comportamento -----------------------------------------------
    gasto_cat = obs.groupby(["cliente_id", "categoria"])["valor"].sum()
    share = (gasto_cat / gasto_cat.groupby(level=0).sum()).groupby(level=0).max()
    f["share_categoria_top"] = f["cliente_id"].map(share).round(3)

    obs["fim_de_semana"] = obs["data"].dt.dayofweek.isin([5, 6])
    f["pct_fim_de_semana"] = f["cliente_id"].map(
        obs.groupby("cliente_id")["fim_de_semana"].mean()).round(3)

    f["cliente_cadastrado"] = f["cliente_id"].map(
        obs.groupby("cliente_id")["cliente_cadastrado"].max()).astype(int)

    for destino, origem in (("canal_principal", "canal"),
                            ("pagamento_principal", "forma_pagamento"),
                            ("mesorregiao", "mesorregiao")):
        f[destino] = f["cliente_id"].map(
            obs.groupby("cliente_id")[origem].agg(
                lambda s: s.mode().iat[0] if len(s.mode()) else "Desconhecida"))
    f["mesorregiao"] = f["mesorregiao"].fillna("Desconhecida")

    # -- o que só o cadastro do CRM fornece --------------------------
    if dim is not None and len(dim):
        cad = dim.set_index("cliente_id")["cadastrado_em"]
        cad = pd.to_datetime(cad)
        f["dias_desde_cadastro"] = (corte - f["cliente_id"].map(cad)).dt.days
        # quem não está no CRM: usa a primeira compra como proxy da entrada
        f["dias_desde_cadastro"] = f["dias_desde_cadastro"].fillna(
            f["dias_desde_primeira"]).astype(int)
    else:
        f["dias_desde_cadastro"] = f["dias_desde_primeira"]

    return f


def marcar_alvo(f, ava):
    """Quem não aparece na janela de avaliação deu churn."""
    voltaram = set(ava["cliente_id"].unique())
    f = f.copy()
    f["churn"] = (~f["cliente_id"].isin(voltaram)).astype(int)
    return f


def codificar(f, colunas_esperadas=None):
    """
    One-Hot nas categóricas.

    A geografia entra como **mesorregião**, e não como cidade. É a mesma
    informação em granularidade mais grossa: são dez municípios contra seis
    mesorregiões, e com 299 clientes no treino cada coluna binária a menos
    é uma chance a menos de o modelo ajustar ruído. Usar as duas seria
    redundância pura, já que mesorregião é um agrupamento de municípios.

    `colunas_esperadas` é a lista salva no treino. Com ela o resultado é
    forçado a ter as mesmas colunas na mesma ordem: categoria nova é
    descartada, categoria ausente entra zerada. Sem esse alinhamento o
    modelo recebe um vetor com significado trocado e responde sem reclamar.
    """
    enc = pd.get_dummies(f, columns=CATEGORICAS,
                         prefix=["meso", "canal", "pgto"],
                         drop_first=True, dtype=int)
    if colunas_esperadas is not None:
        enc = enc.reindex(columns=colunas_esperadas, fill_value=0)
    return enc


def lista_features(enc):
    return [c for c in enc.columns if c not in DESCARTAR]


def numericas(features):
    return [c for c in features
            if not c.startswith(("meso_", "canal_", "pgto_"))
            and c != "cliente_cadastrado"]
