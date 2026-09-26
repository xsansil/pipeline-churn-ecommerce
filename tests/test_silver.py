"""
Testes da camada silver: limpeza, integração e as garantias do banco.

Dois grupos. O primeiro verifica as regras de limpeza em Python. O segundo
verifica que o banco recusa o que escapar delas. A defesa em profundidade
só vale se alguém já tiver checado que ela existe.

    python -m pytest tests/test_silver.py -v
    python tests/test_silver.py
"""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src import db, limpeza              # noqa: E402


# ---------------------------------------------------------------------
# Regras de limpeza
# ---------------------------------------------------------------------
def _cru(**troca):
    base = {
        "transacao_id": "1", "cliente_id": "1", "nome_cliente": "ana silva",
        "email": "ana@email.com", "idade": "30", "cidade": "  MACAPA",
        "estado": "ap", "data": "2024-03-15", "produto": "MOUSE SEM FIO  ",
        "categoria": "Perifericos", "quantidade": "2", "valor": "180,00",
        "canal": "Site", "forma_pagamento": "Pix",
    }
    base.update(troca)
    return pd.DataFrame([base])


def test_valor_brasileiro_vira_numero():
    df = limpeza.limpar(_cru(valor="180,00"))
    assert len(df) == 1
    assert float(df.iloc[0]["valor"]) == 180.0


def test_texto_e_padronizado():
    df = limpeza.limpar(_cru())
    linha = df.iloc[0]
    assert linha["cidade"] == "Macapa"
    assert linha["produto"] == "Mouse Sem Fio"
    assert linha["estado"] == "AP"
    assert linha["nome_cliente"] == "Ana Silva"


def test_descarta_o_que_nao_da_para_corrigir():
    for rotulo, troca in [("data impossível", {"data": "31/02/2024"}),
                          ("data ausente", {"data": ""}),
                          ("valor ausente", {"valor": ""}),
                          ("valor negativo", {"valor": "-90.00"}),
                          ("quantidade zero", {"quantidade": "0"}),
                          ("quantidade absurda", {"quantidade": "999"})]:
        assert len(limpeza.limpar(_cru(**troca))) == 0, rotulo


def test_outlier_e_pelo_preco_unitario_nao_pelo_total():
    """
    Um monitor caro é venda legítima; o mesmo monitor com o valor
    multiplicado por mil é erro de digitação. O critério tem de distinguir.
    """
    legitima = _cru(produto="Monitor 24 polegadas", quantidade="4", valor="3600.00")
    assert len(limpeza.limpar(legitima)) == 1, "venda cara e válida foi descartada"

    digitacao = _cru(produto="Monitor 24 polegadas", quantidade="1", valor="900000.00")
    assert len(limpeza.limpar(digitacao)) == 0, "erro de digitação passou"


def test_email_malformado_e_marcado_nao_descartado():
    df = limpeza.limpar(_cru(email="anaemail.com"))
    assert len(df) == 1, "a venda tem de permanecer"
    assert not bool(df.iloc[0]["email_valido"])


def test_duplicata_exata_sai():
    dobrado = pd.concat([_cru(), _cru()], ignore_index=True)
    assert len(limpeza.limpar(dobrado)) == 1


def test_sem_acento_casa_as_variacoes():
    for v in ("Macapá", "MACAPA", "  macapa", "Macapa  "):
        assert limpeza.sem_acento(v) == "macapa"


# ---------------------------------------------------------------------
# Garantias do banco
# ---------------------------------------------------------------------
INVALIDAS = [
    ("quantidade 0", {"quantidade": 0}),
    ("quantidade 999", {"quantidade": 999}),
    ("valor negativo", {"valor": -10}),
    ("valor zero", {"valor": 0}),
    ("preco unitario zero", {"preco_unitario": 0}),
    ("idade 200", {"idade": 200}),
]


def _linha_valida():
    return {
        "transacao_id": 999999999, "cliente_id": 1, "nome_cliente": "Teste",
        "email": "t@e.com", "email_valido": True, "idade": 30,
        "cidade": "Macapa", "estado": "AP", "mesorregiao": "Sul do Amapá",
        "data": "2024-03-15", "produto": "Mouse Sem Fio",
        "categoria": "Perifericos", "quantidade": 1, "valor": 90,
        "preco_unitario": 90, "custo": 52.2, "margem": 37.8,
        "canal": "Site", "forma_pagamento": "Pix", "cliente_cadastrado": True,
    }


def test_banco_recusa_linha_invalida():
    """
    Se a limpeza em Python tiver um furo, o CHECK do banco barra. As duas
    guardas são independentes de propósito.
    """
    eng = db.engine()
    colunas = ", ".join(_linha_valida())
    marcas = ", ".join(":" + c for c in _linha_valida())
    sql = text("insert into silver.transacoes (%s) values (%s)" % (colunas, marcas))

    for rotulo, troca in INVALIDAS:
        linha = _linha_valida()
        linha.update(troca)
        recusou = False
        try:
            with eng.begin() as c:
                c.execute(sql, linha)
        except IntegrityError:
            recusou = True
        else:                                   # entrou: desfaz e acusa
            with eng.begin() as c:
                c.execute(text("delete from silver.transacoes where transacao_id = 999999999"))
        assert recusou, "o banco aceitou %s" % rotulo


def test_chave_primaria_barra_duplicata():
    eng = db.engine()
    linha = _linha_valida()
    colunas = ", ".join(linha)
    marcas = ", ".join(":" + c for c in linha)
    sql = text("insert into silver.transacoes (%s) values (%s)" % (colunas, marcas))
    try:
        with eng.begin() as c:
            c.execute(sql, linha)
        repetiu = False
        try:
            with eng.begin() as c:
                c.execute(sql, linha)
        except IntegrityError:
            repetiu = True
        assert repetiu, "a chave primária deixou passar uma duplicata"
    finally:
        with eng.begin() as c:
            c.execute(text("delete from silver.transacoes where transacao_id = 999999999"))


def test_integracao_bateu_as_contas():
    """Os números que a entrega documenta têm de sair do banco."""
    eng = db.engine()
    with eng.connect() as c:
        r = c.execute(text("""
            select count(*),
                   count(distinct cliente_id),
                   count(*) filter (where not cliente_cadastrado),
                   count(*) filter (where mesorregiao is not null),
                   count(*) filter (where custo is null)
              from silver.transacoes""")).one()
    linhas, clientes, sem_cadastro, com_meso, sem_custo = r
    assert linhas == 5809
    assert clientes == 399
    assert sem_cadastro == 135, "as transações sem cadastro mudaram"
    assert com_meso >= linhas - 1, "o join com o IBGE degradou"
    assert sem_custo == 0, "o catálogo deixou produto sem custo"


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
