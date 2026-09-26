"""
Regras de limpeza (bronze para silver).

Fonte única das regras. O ETL usa este módulo, e a API também: se houvesse
duas implementações, elas divergiriam com o tempo e o modelo passaria a
receber em produção dados diferentes dos que aprendeu, sem erro nenhum,
só com previsões piores.

O critério geral: o que dá para corrigir é corrigido, o que não dá é
descartado, e nada é inventado. Data e valor não têm como ser imputados
sem criar informação falsa, então o registro sai. Idade e cidade não são
essenciais à transação, então são preenchidas.
"""
import logging
import unicodedata

import pandas as pd

from . import config

log = logging.getLogger("pipeline")

TEXTO_TITULO = ["cidade", "produto", "categoria", "canal", "forma_pagamento",
                "nome_cliente"]


def sem_acento(s):
    """'Macapá' -> 'macapa'. Chave de junção entre fontes que grafam diferente."""
    if s is None:
        return ""
    normal = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in normal if not unicodedata.combining(c)).strip().lower()


def limpar(df, relatorio=None):
    """
    Recebe o dado cru da bronze e devolve as transações utilizáveis.

    `relatorio` recebe tuplas (rótulo, entrada, saída, removidos). É o que
    alimenta o funil documentado na entrega.
    """
    passos = []

    def marca(rotulo, antes, depois):
        passos.append((rotulo, antes, depois, antes - depois))

    df = df.copy()

    # 1. duplicatas exatas. Vem antes da conversão de tipos de propósito:
    # depois dela, "450,00" e "450.00" viram o mesmo número e a contagem de
    # duplicatas mudaria conforme o momento em que fosse medida.
    n = len(df)
    df = df.drop_duplicates(subset=[c for c in df.columns
                                    if not c.startswith("_")])
    marca("duplicatas exatas", n, len(df))

    # 2. conversão de tipos. O que não converter vira nulo e cai adiante.
    df["data"] = pd.to_datetime(df["data"], errors="coerce", format="mixed")
    df["valor"] = pd.to_numeric(
        df["valor"].astype(str).str.replace(",", ".", regex=False), errors="coerce")
    df["quantidade"] = pd.to_numeric(df["quantidade"], errors="coerce")
    df["idade"] = pd.to_numeric(df["idade"], errors="coerce")
    df["cliente_id"] = pd.to_numeric(df["cliente_id"], errors="coerce")
    df["transacao_id"] = pd.to_numeric(df["transacao_id"], errors="coerce")

    n = len(df)
    df = df[df["data"].notna()]
    marca("data ausente ou impossível", n, len(df))

    n = len(df)
    df = df[df["valor"].notna()]
    marca("valor ausente ou não numérico", n, len(df))

    n = len(df)
    df = df[df["valor"] > 0]
    marca("valor <= 0 (estorno como venda)", n, len(df))

    n = len(df)
    df = df[df["quantidade"].between(1, 20)]
    marca("quantidade fora de 1..20", n, len(df))

    # 6. outlier real: erro de digitação multiplica o valor por mil.
    # O critério é o preço unitário contra o teto do catálogo, não o IQR
    # sobre o total: o IQR eliminaria monitores e SSDs, que são caros e
    # legítimos, e é a categoria de maior receita.
    n = len(df)
    df = df.copy()
    df["preco_unitario"] = (df["valor"] / df["quantidade"]).round(2)
    df = df[df["preco_unitario"] <= config.TETO_PRECO_UNITARIO]
    marca("preço unitário implausível", n, len(df))

    # 7. padronização de texto. O fillna("") antes do astype(str) é
    # obrigatório no pandas 3: nessa versão o astype(str) PRESERVA os
    # ausentes como NA, e a coluna ficaria com float e str misturados.
    df = df.copy()
    for coluna in TEXTO_TITULO:
        if coluna in df.columns:
            df[coluna] = df[coluna].fillna("").astype(str).str.strip().str.title()
    for coluna, caixa in (("email", "lower"), ("estado", "upper")):
        if coluna in df.columns:
            df[coluna] = getattr(
                df[coluna].fillna("").astype(str).str.strip().str, caixa)()

    # e-mail malformado é marcado, não descartado: a venda aconteceu
    df["email_valido"] = df["email"].str.contains(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$", regex=True, na=False)

    df["quantidade"] = df["quantidade"].astype(int)
    df["cliente_id"] = df["cliente_id"].astype(int)
    df["transacao_id"] = df["transacao_id"].astype("int64")

    if relatorio is not None:
        relatorio.extend(passos)
    return df


def imputar(df, relatorio=None):
    """
    Preenche o que sobrou de ausente. Roda **depois** do enriquecimento,
    para que o cadastro do CRM tenha a chance de fornecer o valor real
    antes de recorrermos à mediana ou a um rótulo genérico.
    """
    passos = []

    faltam_idade = int(df["idade"].isna().sum())
    if faltam_idade:
        mediana = df["idade"].median()
        df["idade"] = df["idade"].fillna(mediana)
        passos.append(("idade pela mediana (%.0f anos)" % mediana, faltam_idade))
    df["idade"] = df["idade"].astype(int)

    vazias = df["cidade"].isin(["", "Nan", "None"]) | df["cidade"].isna()
    if vazias.any():
        df.loc[vazias, "cidade"] = "Desconhecida"
        passos.append(("cidade marcada como Desconhecida", int(vazias.sum())))

    if relatorio is not None:
        relatorio.extend(passos)
    return df
