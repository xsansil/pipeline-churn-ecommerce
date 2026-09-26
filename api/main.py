"""
API do projeto integrador: o pipeline exposto por HTTP.

    pip install -r requirements.txt
    uvicorn api.main:app --reload
    http://127.0.0.1:8000/docs

Duas famílias de rota:

  * **consulta**: leem as views do gold. A carteira ordenada por risco, o
    desempenho dos modelos ao longo das rodadas, a contribuição de cada
    fonte. O trabalho é do banco; a API só serve o resultado.
  * **previsão**: recebem o histórico de um cliente e percorrem o mesmo
    caminho do treino: limpeza, enriquecimento pelo banco, features,
    padronização, modelo.

A API não reimplementa nada. Importa `limpeza`, `enriquecimento` e
`features`, os mesmos módulos que o ETL usa. Duas implementações da
mesma regra divergem com o tempo, e o modelo passa a receber em produção
dados diferentes dos que aprendeu, sem erro nenhum.
"""
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src import config, db, enriquecimento, features, limpeza   # noqa: E402
from src.modelo import treinar                                   # noqa: E402

# ---------------------------------------------------------------------
# Carga dos artefatos: uma vez, na subida
# ---------------------------------------------------------------------
CAMINHO = config.MODELOS / treinar.ARQUIVO
if not CAMINHO.exists():
    raise RuntimeError(
        "Modelo não encontrado em %s. Rode antes: python pipeline.py" % CAMINHO)

PACOTE = joblib.load(CAMINHO)
MODELO = PACOTE["modelo"]
SCALER = PACOTE["scaler"]
FEATURES = PACOTE["features"]
NUMERICAS = PACOTE["numericas"]
LIMIAR = PACOTE["limiar"]

app = FastAPI(
    title="Previsão de churn em e-commerce de informática",
    description="Projeto Integrador · Fundamentos de Banco de Dados "
                "(BDED-2026.1) · UNIFAP Digital",
    version="1.0.0",
)


def engine():
    try:
        return db.engine()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


def consulta(sql, **params):
    """
    Executa a consulta e devolve registros prontos para JSON.

    O `astype(object).where(notna)` não é enfeite: NULL do banco vira NaN no
    pandas, e NaN é float. O serializador JSON rejeita com "Out of range
    float values are not JSON compliant" e a rota devolve 500. Converter
    para None antes é o que preserva o NULL como `null` na resposta.
    """
    df = db.le(sql, engine(), **params)
    return df.astype(object).where(pd.notna(df), None).to_dict(orient="records")


# =====================================================================
# Status
# =====================================================================
@app.get("/", tags=["status"])
def raiz():
    return {"servico": "previsão de churn", "documentacao": "/docs"}


@app.get("/saude", tags=["status"])
def saude():
    """Modelo carregado e banco alcançável."""
    try:
        with db.engine().connect() as c:
            c.execute(text("select 1"))
        banco = "ok"
    except Exception as e:
        banco = "indisponível: %s" % str(e).splitlines()[0][:80]
    return {"modelo": PACOTE["nome"], "features": len(FEATURES),
            "treinado_em": PACOTE["treinado_em"], "banco": banco}


@app.get("/modelo", tags=["status"])
def info_modelo():
    """Metadados do modelo vigente, do artefato e do registry."""
    m = PACOTE["metricas"]
    return {
        "nome": PACOTE["nome"],
        "treinado_em": PACOTE["treinado_em"],
        "execucao": PACOTE["execucao"],
        "corte_temporal": PACOTE["corte_temporal"],
        "limiar": LIMIAR,
        "n_features": len(FEATURES),
        "metricas_no_teste": {k: round(float(v), 4) for k, v in m.items()
                              if k != "modelo"},
        "linha_de_base": {k: round(float(v), 4)
                          for k, v in PACOTE["linha_de_base"].items()},
        "nota": "A seleção usou ROC AUC. Nesta base a linha de base obtém "
                "F1 de %.3f, alto o bastante para mascarar um modelo ruim."
                % PACOTE["linha_de_base"]["f1"],
    }


@app.get("/features", tags=["status"])
def listar_features():
    """A ordem desta lista é o contrato de entrada do modelo."""
    return {"total": len(FEATURES), "features": FEATURES,
            "padronizadas": NUMERICAS}


# =====================================================================
# Consulta: lê as views do gold
# =====================================================================
@app.get("/clientes/risco", tags=["consulta"])
def carteira_em_risco(
    limite: int = Query(20, ge=1, le=500),
    faixa: Optional[str] = Query(None, description="alto, médio, baixo, muito baixo"),
    conjunto: Optional[str] = Query(None, description="treino ou teste"),
):
    """A carteira ordenada pela probabilidade de churn: a fila de abordagem."""
    onde, params = [], {"limite": limite}
    if faixa:
        onde.append("faixa_de_risco = :faixa")
        params["faixa"] = faixa
    if conjunto:
        onde.append("conjunto = :conjunto")
        params["conjunto"] = conjunto
    filtro = (" where " + " and ".join(onde)) if onde else ""
    return consulta(
        "select * from gold.vw_clientes_risco%s order by posicao limit :limite"
        % filtro, **params)


@app.get("/clientes/{cliente_id}", tags=["consulta"])
def cliente(cliente_id: int):
    """Previsão e principais features de um cliente."""
    linhas = consulta(
        "select * from gold.vw_clientes_risco where cliente_id = :id",
        id=cliente_id)
    if not linhas:
        raise HTTPException(404, "Cliente %d não está na última execução."
                                 % cliente_id)
    return linhas[0]


@app.get("/regioes", tags=["consulta"])
def regioes():
    """Risco e receita por mesorregião, que só existe por causa da API do IBGE."""
    return consulta("select * from gold.vw_risco_por_regiao "
                    "order by receita_em_risco desc nulls last")


@app.get("/metricas/modelos", tags=["consulta"])
def desempenho_modelos():
    """Desempenho por algoritmo ao longo das rodadas, lido do registry."""
    return consulta("select * from gold.vw_desempenho_modelos")


@app.get("/metricas/decis", tags=["consulta"])
def ganho_por_decil():
    """
    Concentração de churn por decil de risco.

    A coluna `pct_churn_teste` é a que vale: a geral inclui os clientes que
    o modelo viu no treino e sai otimista.
    """
    return consulta("select * from gold.vw_ganho_por_decil")


@app.get("/metricas/fontes", tags=["consulta"])
def contribuicao_das_fontes():
    """Quantas features cada fonte gerou e o quanto elas se movem com o alvo."""
    return consulta("select * from gold.vw_contribuicao_das_fontes")


@app.get("/execucoes", tags=["consulta"])
def execucoes(limite: int = Query(30, ge=1, le=200)):
    """Últimos passos registrados pelo ETL: a proveniência."""
    return consulta(
        "select momento, etapa, fase, fonte, quantidade, detalhe "
        "from meta.etl_execucoes order by id desc limit :limite", limite=limite)


# =====================================================================
# Previsão
# =====================================================================
class Transacao(BaseModel):
    data: date
    valor: float
    quantidade: int
    produto: str
    categoria: str
    canal: str
    forma_pagamento: str
    cidade: str
    estado: str = Field(..., min_length=2, max_length=2)
    idade: int


class Pedido(BaseModel):
    cliente_id: int = Field(..., examples=[1])
    data_referencia: Optional[date] = Field(
        None, description="Data a partir da qual a recência é contada. Sem ela, "
                          "usa-se hoje. Para conferir contra a base de 2024, "
                          "informe 2024-10-01.")
    transacoes: List[Transacao] = Field(..., min_length=1)


class Resposta(BaseModel):
    cliente_id: int
    probabilidade_churn: float
    classificacao: str
    faixa_de_risco: str
    limiar: float
    transacoes_recebidas: int
    transacoes_validas: int
    data_referencia: date
    modelo: str
    diagnostico: dict


@app.post("/prever", response_model=Resposta, tags=["previsão"])
def prever(pedido: Pedido):
    """
    Recebe o histórico de compras e percorre o mesmo caminho do treino:
    limpeza, enriquecimento pelo banco, features, padronização, modelo.
    """
    eng = engine()

    bruto = pd.DataFrame([t.model_dump() for t in pedido.transacoes])
    bruto["cliente_id"] = pedido.cliente_id
    bruto["transacao_id"] = range(1, len(bruto) + 1)
    bruto["nome_cliente"] = ""
    bruto["email"] = ""
    recebidas = len(bruto)

    limpo = limpeza.limpar(bruto)
    if limpo.empty:
        raise HTTPException(
            422, "Nenhuma transação sobreviveu à limpeza. Verifique datas, "
                 "valores e quantidades.")

    try:
        limpo, diag = enriquecimento.aplicar(limpo, eng)
    except ValueError as e:
        raise HTTPException(422, str(e))

    referencia = pd.Timestamp(pedido.data_referencia or date.today())
    obs = limpo[pd.to_datetime(limpo["data"]) < referencia]
    if obs.empty:
        raise HTTPException(
            422, "Todas as transações são posteriores à data de referência "
                 "(%s); a recência não pode ser calculada." % referencia.date())

    dim = db.le("select * from silver.clientes where cliente_id = :id",
                eng, id=pedido.cliente_id)
    obs = obs.copy()
    obs["cliente_cadastrado"] = bool(len(dim))

    f = features.construir(obs, dim=dim if len(dim) else None, corte=referencia)
    X = features.codificar(f, colunas_esperadas=FEATURES)
    X[NUMERICAS] = SCALER.transform(X[NUMERICAS])

    p = float(MODELO.predict_proba(X)[0, 1])
    diag["cliente_no_crm"] = bool(len(dim))
    return Resposta(
        cliente_id=pedido.cliente_id,
        probabilidade_churn=round(p, 4),
        classificacao="churn" if p >= LIMIAR else "ativo",
        faixa_de_risco=treinar.faixa(p),
        limiar=LIMIAR,
        transacoes_recebidas=recebidas,
        transacoes_validas=len(obs),
        data_referencia=referencia.date(),
        modelo=PACOTE["nome"],
        diagnostico=diag,
    )
