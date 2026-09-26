"""
Enriquecimento das transações com as fontes de apoio.

Extraído para módulo próprio porque dois caminhos precisam dele: o ETL, ao
montar a camada silver, e a API, ao pontuar um cliente novo. Se cada um
tivesse a própria implementação, elas divergiriam, e a diferença
apareceria como previsão pior, não como erro.

As duas junções são determinísticas e dependem só do banco:

    catálogo (bronze.catalogo)          custo unitário -> margem
    IBGE     (bronze.municipios_ibge)   mesorregião do município

A chave é sempre o texto normalizado sem acento. O IBGE devolve "Macapá",
o extrato traz "MACAPA", "  macapa" ou "Macapa  "; sem normalizar, o join
casa quase nada e não reclama.
"""
import pandas as pd

from . import db, limpeza


def catalogo(eng):
    cat = db.le("select produto, custo from bronze.catalogo", eng)
    cat["chave_produto"] = cat["produto"].map(limpeza.sem_acento)
    cat["custo_unitario"] = pd.to_numeric(cat["custo"])
    return cat[["chave_produto", "custo_unitario"]]


def municipios(eng):
    ibge = db.le("select municipio, uf, mesorregiao from bronze.municipios_ibge", eng)
    ibge["chave_cidade"] = ibge["municipio"].map(limpeza.sem_acento)
    return (ibge.drop_duplicates(subset=["chave_cidade", "uf"])
                [["chave_cidade", "uf", "mesorregiao"]])


def aplicar(df, eng, exigir_catalogo=True):
    """
    Acrescenta custo, margem e mesorregião. Devolve (df, diagnóstico).

    O diagnóstico traz a contagem de cada junção. `merge(how="left")` nunca
    levanta erro: quando a chave não casa, preenche nulo e segue. Contar é
    o que transforma uma falha silenciosa em informação.
    """
    df = df.copy()
    diag = {}

    # -- custo e margem ----------------------------------------------
    cat = catalogo(eng)
    df["chave_produto"] = df["produto"].map(limpeza.sem_acento)
    antes = len(df)
    df = df.merge(cat, on="chave_produto", how="left")
    assert len(df) == antes, "o merge com o catálogo duplicou linhas"

    diag["sem_custo"] = int(df["custo_unitario"].isna().sum())
    if diag["sem_custo"] and exigir_catalogo:
        faltando = sorted(df.loc[df["custo_unitario"].isna(), "produto"].unique())
        raise ValueError(
            "Produtos fora do catálogo: %s. Sem o custo a margem fica errada "
            "em silêncio." % faltando)

    df["custo"] = (df["custo_unitario"] * df["quantidade"]).round(2)
    df["margem"] = (df["valor"] - df["custo"]).round(2)

    # -- mesorregião --------------------------------------------------
    ibge = municipios(eng)
    df["chave_cidade"] = df["cidade"].map(limpeza.sem_acento)
    antes = len(df)
    df = df.merge(ibge, left_on=["chave_cidade", "estado"],
                  right_on=["chave_cidade", "uf"], how="left")
    assert len(df) == antes, "o merge com o IBGE duplicou linhas"

    diag["com_mesorregiao"] = int(df["mesorregiao"].notna().sum())
    diag["sem_mesorregiao"] = int(df["mesorregiao"].isna().sum())
    diag["cidades_sem_regiao"] = sorted(
        df.loc[df["mesorregiao"].isna(), "cidade"].unique().tolist())

    return df, diag
