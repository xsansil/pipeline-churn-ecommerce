"""
Testes do treino e do artefato produzido.

O foco é o que costuma quebrar em silêncio depois que o modelo sai do
notebook: a ordem das colunas, o padronizador esquecido e a métrica de
seleção trocada.

    python -m pytest tests/test_modelo.py -v
    python tests/test_modelo.py
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import text

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src import config, db, features        # noqa: E402
from src.modelo import treinar              # noqa: E402


def _pacote():
    caminho = config.MODELOS / treinar.ARQUIVO
    assert caminho.exists(), "rode: python pipeline.py --etapa modelo"
    return joblib.load(caminho)


def _amostra(n=30):
    enc = db.le("select * from gold.features_clientes order by cliente_id limit %d" % n,
                db.engine())
    return enc


# ---------------------------------------------------------------------
# O artefato
# ---------------------------------------------------------------------
def test_artefato_traz_tudo_o_que_precisa():
    """
    Modelo e scaler no mesmo arquivo. Como artefato separado, o scaler é a
    peça que se esquece — e o modelo aceita valores crus sem reclamar.
    """
    p = _pacote()
    for chave in ("modelo", "scaler", "features", "numericas", "limiar",
                  "metricas", "treinado_em", "corte_temporal"):
        assert chave in p, "falta %s no artefato" % chave
    assert len(p["features"]) == 44
    assert p["scaler"].n_features_in_ == len(p["numericas"])


def test_ordem_das_features_e_respeitada():
    """
    Embaralhar as colunas não pode mudar a previsão — desde que o consumidor
    reindexe pela lista salva. Sem isso o modelo recebe um vetor com
    significado trocado e responde assim mesmo.
    """
    p = _pacote()
    enc = _amostra()
    X = enc[p["features"]].copy()
    X[p["numericas"]] = p["scaler"].transform(X[p["numericas"]])
    esperado = p["modelo"].predict_proba(X)[:, 1]

    embaralhado = enc[list(reversed(p["features"]))]
    X2 = embaralhado[p["features"]].copy()          # reindexa de volta
    X2[p["numericas"]] = p["scaler"].transform(X2[p["numericas"]])
    obtido = p["modelo"].predict_proba(X2)[:, 1]

    assert np.allclose(esperado, obtido)


def test_sem_o_scaler_a_previsao_muda():
    """
    Demonstra por que o scaler é parte do artefato: aplicar o modelo em
    valores crus não levanta erro nenhum, só devolve outra resposta.
    """
    p = _pacote()
    enc = _amostra()
    cru = enc[p["features"]].copy()
    escalado = cru.copy()
    escalado[p["numericas"]] = p["scaler"].transform(cru[p["numericas"]])

    prob_cru = p["modelo"].predict_proba(cru)[:, 1]
    prob_ok = p["modelo"].predict_proba(escalado)[:, 1]

    assert not np.allclose(prob_cru, prob_ok), \
        "se der igual, o scaler não está fazendo nada e algo está errado"
    assert np.abs(prob_cru - prob_ok).max() > 0.1


# ---------------------------------------------------------------------
# A escolha do modelo
# ---------------------------------------------------------------------
def test_modelo_supera_a_linha_de_base():
    p = _pacote()
    assert p["metricas"]["roc_auc"] > 0.65, "o modelo mal supera o acaso"
    assert p["linha_de_base"]["roc_auc"] == 0.5


def test_a_armadilha_do_f1_continua_valendo():
    """
    Documenta por que a seleção usa ROC AUC. Se este teste falhar, é porque
    o conjunto mudou e a justificativa precisa ser revista — não porque o
    código quebrou.
    """
    p = _pacote()
    f1_base = p["linha_de_base"]["f1"]
    assert f1_base > 0.70, \
        "o F1 da linha de base caiu para %.3f; reveja a metrica de selecao" % f1_base
    assert p["linha_de_base"]["roc_auc"] == 0.5, \
        "em ROC AUC a linha de base tem de ficar exatamente no acaso"


def test_selecao_foi_por_roc_auc():
    """O escolhido tem de ser o de maior ROC AUC da rodada, não o de maior F1."""
    eng = db.engine()
    with eng.connect() as c:
        ultima = c.execute(text(
            "select execucao from meta.treinos order by treinado_em desc limit 1")).scalar()
        linhas = pd.read_sql(text(
            "select modelo, escolhido, roc_auc, f1 from meta.treinos where execucao = :e"),
            eng, params={"e": ultima})
    escolhido = linhas[linhas.escolhido].iloc[0]
    assert escolhido["roc_auc"] == linhas["roc_auc"].max()


# ---------------------------------------------------------------------
# O registro no banco
# ---------------------------------------------------------------------
def test_registry_guarda_todos_os_modelos_da_rodada():
    eng = db.engine()
    with eng.connect() as c:
        r = c.execute(text("""
            select count(*), count(*) filter (where escolhido)
              from meta.treinos
             where execucao = (select execucao from meta.treinos
                                order by treinado_em desc limit 1)""")).one()
    assert r[0] == 5, "a rodada deveria ter os cinco modelos"
    assert r[1] == 1, "só um modelo pode estar marcado como escolhido"


def test_previsoes_cobrem_a_carteira():
    eng = db.engine()
    with eng.connect() as c:
        r = c.execute(text("""
            select count(*), min(probabilidade), max(probabilidade)
              from gold.previsoes
             where execucao = (select execucao from meta.treinos
                                order by treinado_em desc limit 1)""")).one()
    assert r[0] == 399, "faltou cliente na previsão"
    assert 0 <= float(r[1]) and float(r[2]) <= 1


def test_treino_e_reprodutivel():
    """Mesma semente, mesmas métricas — é o que permite comparar rodadas."""
    eng = db.engine()
    with eng.connect() as c:
        desvios = c.execute(text("""
            select coalesce(max(d), 0) from (
              select stddev_samp(roc_auc) d from meta.treinos group by modelo) x""")).scalar()
    assert float(desvios) < 1e-6, \
        "rodadas com a mesma semente divergiram: %s" % desvios


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
            print("  FALHOU  %s  %s" % (t.__name__, e))
        except Exception as e:
            falhas += 1
            print("  ERRO    %s  %s: %s" % (t.__name__, type(e).__name__, e))
    print()
    print("%d de %d passaram" % (len(testes) - falhas, len(testes)))
    sys.exit(1 if falhas else 0)
