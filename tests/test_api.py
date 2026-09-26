"""
Testes da API.

Usam o TestClient do FastAPI, que roda a aplicação em processo, então não é
preciso subir o uvicorn.

    python -m pytest tests/test_api.py -v
    python tests/test_api.py
"""
import sys
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from api.main import app                    # noqa: E402
from src import db                          # noqa: E402

c = TestClient(app)
COLUNAS = ["data", "valor", "quantidade", "produto", "categoria", "canal",
           "forma_pagamento", "cidade", "estado", "idade"]


def _historico(cliente_id, ate="2024-10-01"):
    tx = db.le("select * from silver.transacoes where cliente_id = :i and data < :d",
               db.engine(), i=cliente_id, d=ate)
    tx = tx[COLUNAS].copy()
    tx["data"] = pd.to_datetime(tx["data"]).dt.strftime("%Y-%m-%d")
    for col in ("quantidade", "idade"):
        tx[col] = tx[col].astype(int)
    tx["valor"] = tx["valor"].astype(float)
    return {"cliente_id": int(cliente_id), "data_referencia": ate,
            "transacoes": tx.to_dict("records")}


# ---------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------
def test_saude():
    r = c.get("/saude")
    assert r.status_code == 200
    assert r.json()["banco"] == "ok"


def test_modelo_expoe_a_linha_de_base():
    """Métrica sem referência não significa nada; a rota entrega as duas."""
    j = c.get("/modelo").json()
    assert j["metricas_no_teste"]["roc_auc"] > j["linha_de_base"]["roc_auc"]
    assert j["linha_de_base"]["roc_auc"] == 0.5


# ---------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------
def test_carteira_vem_ordenada_por_risco():
    linhas = c.get("/clientes/risco?limite=25").json()
    assert len(linhas) == 25
    probs = [l["probabilidade"] for l in linhas]
    assert probs == sorted(probs, reverse=True)
    assert linhas[0]["posicao"] == 1


def test_filtro_por_conjunto():
    """Poder isolar o teste é o que permite ler a carteira sem otimismo."""
    so_teste = c.get("/clientes/risco?limite=200&conjunto=teste").json()
    assert len(so_teste) == 100
    assert {l["conjunto"] for l in so_teste} == {"teste"}


def test_cliente_inexistente_da_404():
    assert c.get("/clientes/999999").status_code == 404


def test_decis_separam_treino_de_teste():
    decis = c.get("/metricas/decis").json()
    assert len(decis) == 10
    assert decis[0]["decil"] == 1
    # o primeiro decil tem de concentrar mais churn que o último
    assert decis[0]["pct_churn_geral"] > decis[-1]["pct_churn_geral"]
    assert all("pct_churn_teste" in d for d in decis)


def test_contribuicao_das_fontes_lista_as_quatro():
    fontes = {f["origem"] for f in c.get("/metricas/fontes").json()}
    assert "extrato CSV" in fontes
    assert "API IBGE" in fontes
    assert "catalogo JSON" in fontes
    assert "CRM PostgreSQL" in fontes


# ---------------------------------------------------------------------
# Previsão
# ---------------------------------------------------------------------
def test_previsao_reproduz_o_que_esta_no_banco():
    """
    O caminho da API e o do ETL têm de dar o mesmo número. Se divergirem, é
    porque alguma regra foi reimplementada em um dos lados.
    """
    alvo = db.le("""select cliente_id, probabilidade from gold.vw_clientes_risco
                     where nome <> '(sem cadastro no CRM)' order by posicao limit 5""",
                 db.engine())
    for _, linha in alvo.iterrows():
        cid = int(linha["cliente_id"])
        r = c.post("/prever", json=_historico(cid))
        assert r.status_code == 200, r.text
        obtido = r.json()["probabilidade_churn"]
        esperado = float(linha["probabilidade"])
        assert abs(obtido - esperado) < 0.01, \
            "cliente %d: API %.4f x banco %.4f" % (cid, obtido, esperado)


def test_previsao_sem_cadastro_no_crm_funciona():
    sem = db.le("""select cliente_id from gold.vw_clientes_risco
                    where nome = '(sem cadastro no CRM)' limit 1""", db.engine())
    if sem.empty:
        return
    r = c.post("/prever", json=_historico(int(sem.iloc[0]["cliente_id"])))
    assert r.status_code == 200
    assert r.json()["diagnostico"]["cliente_no_crm"] is False


def test_transacoes_todas_no_futuro_dao_422():
    pedido = _historico(2)
    pedido["data_referencia"] = "2023-01-01"
    r = c.post("/prever", json=pedido)
    assert r.status_code == 422
    assert "recência" in r.json()["detail"]


def test_produto_fora_do_catalogo_e_recusado():
    """Sem custo a margem sai errada em silêncio; melhor recusar."""
    pedido = _historico(2)
    pedido["transacoes"][0]["produto"] = "Produto Inexistente"
    r = c.post("/prever", json=pedido)
    assert r.status_code == 422
    assert "catálogo" in r.json()["detail"]


# ---------------------------------------------------------------------
if __name__ == "__main__":
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    falhas = 0
    for t in testes:
        try:
            t()
            print("  ok      %s" % t.__name__)
        except AssertionError as e:
            falhas += 1
            print("  FALHOU  %s  %s" % (t.__name__, str(e)[:110]))
        except Exception as e:
            falhas += 1
            print("  ERRO    %s  %s: %s" % (t.__name__, type(e).__name__, str(e)[:90]))
    print()
    print("%d de %d passaram" % (len(testes) - falhas, len(testes)))
    sys.exit(1 if falhas else 0)
