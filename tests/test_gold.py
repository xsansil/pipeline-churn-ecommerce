"""
Testes da camada gold — sobretudo contra vazamento de dados.

Vazamento é a falha mais cara deste tipo de projeto porque ela não se
manifesta como erro: o modelo fica ótimo nas métricas e inútil na prática.
Os testes aqui existem para que a separação temporal continue valendo
mesmo depois que alguém mexer nas features daqui a seis meses.

    python -m pytest tests/test_gold.py -v
    python tests/test_gold.py
"""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src import config, db, features        # noqa: E402

LIMIAR_SUSPEITO = 0.90


def _silver():
    eng = db.engine()
    return (db.le("select * from silver.transacoes", eng),
            db.le("select * from silver.clientes", eng))


# ---------------------------------------------------------------------
# Separação temporal
# ---------------------------------------------------------------------
def test_janelas_nao_se_sobrepoem():
    tx, _ = _silver()
    obs, ava, corte = features.separar_janelas(tx)
    assert obs["data"].max() < corte
    assert ava["data"].min() >= corte
    assert len(obs) + len(ava) == len(tx), "alguma transação sumiu na divisão"


def test_features_nao_enxergam_a_janela_de_avaliacao():
    """
    O teste que importa: construir as features com a janela de observação
    inteira e com ela mais a de avaliação tem de dar resultados diferentes.
    Se der igual, é porque a função ignora o corte — e nesse caso ela
    estaria livre para olhar o futuro sem que ninguém percebesse.
    """
    tx, dim = _silver()
    obs, ava, corte = features.separar_janelas(tx)

    so_observacao = features.construir(obs, dim=dim, corte=corte)
    com_futuro = features.construir(pd.concat([obs, ava]), dim=dim, corte=corte)

    a = so_observacao.set_index("cliente_id")["qtd_compras"]
    b = com_futuro.set_index("cliente_id")["qtd_compras"]
    assert not a.equals(b.reindex(a.index)), \
        "as features não mudaram ao receber o futuro: o corte não está sendo aplicado"


def test_recencia_nunca_e_negativa():
    """Recência negativa significaria compra depois do corte dentro da feature."""
    tx, dim = _silver()
    obs, _, corte = features.separar_janelas(tx)
    f = features.construir(obs, dim=dim, corte=corte)
    assert (f["recencia_dias"] >= 0).all()
    assert (f["dias_desde_primeira"] >= f["recencia_dias"]).all()


def test_nenhuma_feature_prediz_perfeitamente():
    """
    Canário de vazamento. Correlação acima de 0,90 com o alvo, num problema
    de churn, quase nunca é um achado — é a resposta escondida na pergunta.
    """
    f = db.le("select * from gold.features_clientes", db.engine())
    alvo = f["churn"]
    numericas = f.drop(columns=["cliente_id", "churn", "primeira_compra",
                                "ultima_compra"])
    cor = numericas.corrwith(alvo).abs().sort_values(ascending=False)
    suspeitas = list(cor[cor > LIMIAR_SUSPEITO].index)
    assert not suspeitas, "features suspeitas de vazamento: %s" % suspeitas
    assert cor.iloc[0] < 0.7, "correlação máxima subiu para %.3f" % cor.iloc[0]


# ---------------------------------------------------------------------
# Encoding e alinhamento
# ---------------------------------------------------------------------
def test_one_hot_nao_inventa_ordem():
    tx, dim = _silver()
    obs, _, corte = features.separar_janelas(tx)
    enc = features.codificar(features.construir(obs, dim=dim, corte=corte))
    binarias = [c for c in enc.columns if c.startswith(("meso_", "canal_", "pgto_"))]
    assert binarias, "o One-Hot não criou coluna nenhuma"
    for c in binarias:
        assert set(enc[c].unique()) <= {0, 1}, "%s não é binária" % c


def test_reindex_alinha_categoria_nova_e_ausente():
    """
    Cliente de uma região que não existia no treino não pode quebrar a
    previsão, e coluna que faltar tem de entrar zerada — na ordem certa.
    """
    tx, dim = _silver()
    obs, _, corte = features.separar_janelas(tx)
    f = features.construir(obs, dim=dim, corte=corte)

    esperadas = features.lista_features(features.codificar(f))

    f_estranho = f.head(1).copy()
    f_estranho["mesorregiao"] = "Regiao Que Nao Existe"
    alinhado = features.codificar(f_estranho, colunas_esperadas=esperadas)

    assert list(alinhado.columns) == esperadas, "a ordem das colunas mudou"
    assert not alinhado.isna().any().any(), "sobrou nulo depois do reindex"
    assert alinhado.filter(like="meso_").sum().sum() == 0, \
        "a região desconhecida deveria zerar todas as binárias de região"


# ---------------------------------------------------------------------
# Consistência do que foi gravado
# ---------------------------------------------------------------------
def test_gold_bate_com_a_silver():
    eng = db.engine()
    with eng.connect() as c:
        clientes_gold = c.execute(
            text("select count(*) from gold.features_clientes")).scalar()
        clientes_silver = c.execute(
            text("select count(distinct cliente_id) from silver.transacoes "
                 "where data < :c"), {"c": config.CORTE}).scalar()
    assert clientes_gold == clientes_silver == 399


def test_dicionario_cobre_todas_as_features():
    eng = db.engine()
    f = db.le("select * from gold.features_clientes limit 1", eng)
    dic = db.le("select * from gold.features_dicionario", eng)
    esperadas = set(f.columns) - set(features.DESCARTAR)
    assert set(dic["feature"]) == esperadas, "o dicionário está fora de sincronia"
    assert dic["origem"].nunique() >= 4, "as quatro fontes deviam aparecer na origem"


def test_taxa_de_churn_estavel():
    f = db.le("select churn from gold.features_clientes", db.engine())
    assert abs(f["churn"].mean() - 0.564) < 0.01, "a taxa de churn mudou"


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
