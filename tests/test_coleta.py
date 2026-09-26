"""
Testes da camada de coleta.

O que vale testar aqui não é o caminho feliz. É o que acontece quando a
fonte externa falha. A API do IBGE está fora do meu controle e vai estar
indisponível em algum momento; o pipeline precisa continuar rodando.

    python -m pytest tests/ -v
    python tests/test_coleta.py          (sem pytest instalado)
"""
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src import config                       # noqa: E402
from src.coleta import ibge, transacoes      # noqa: E402

URL_MORTA = "http://127.0.0.1:9/{}"          # porta 9 (discard): recusa na hora


# ---------------------------------------------------------------------
# Coleta externa: resiliência
# ---------------------------------------------------------------------
def test_ibge_api_no_ar():
    """Caminho normal: a API responde e o resultado vira cache."""
    registros, origem = ibge.coletar()
    assert origem == "api"
    assert len(registros) > 200
    assert (config.DADOS / "municipios_ibge.json").exists()
    macapa = [r for r in registros if r["municipio_chave"] == "macapa"]
    assert macapa and macapa[0]["uf"] == "AP"
    assert macapa[0]["mesorregiao"]


def test_ibge_uf_indisponivel_completa_pelo_cache():
    """
    Degradação parcial: uma UF cai, a outra responde.

    Para exercitar mesmo esse caminho é preciso derrubar **só uma** das UFs,
    e não a API inteira, senão o teste cai no ramo do cache e o ramo
    parcial nunca é executado. Daí o patch cirúrgico no requests.get.
    """
    ibge.coletar()                            # garante o cache
    real_get = ibge.requests.get

    def get_com_uf_quebrada(url, **kw):
        if "/RR/" in url:
            raise ibge.requests.ConnectionError("UF derrubada no teste")
        return real_get(url, **kw)

    try:
        ibge.requests.get = get_com_uf_quebrada
        registros, origem = ibge.coletar(ufs=["AP", "RR"], timeout=10)
    finally:
        ibge.requests.get = real_get

    assert origem == "api+cache", "esperava o ramo de degradação parcial"
    ufs = {r["uf"] for r in registros}
    assert "AP" in ufs, "a UF que respondeu tem de entrar"
    assert "RR" in ufs, "a UF que caiu deve ser completada pelo cache"


def test_ibge_fora_do_ar_usa_cache():
    """A API inteira cai: o pipeline segue com o dado de ontem."""
    ibge.coletar()                            # garante o cache
    real = config.IBGE_URL
    try:
        ibge.config.IBGE_URL = URL_MORTA
        registros, origem = ibge.coletar(timeout=3)
    finally:
        ibge.config.IBGE_URL = real
    assert origem == "cache"
    assert len(registros) > 200


def test_sem_acento():
    """A chave de junção precisa casar as variações que vêm do extrato."""
    for entrada in ("Macapá", "MACAPA", "  macapa", "Macapa  "):
        assert ibge.sem_acento(entrada) == "macapa"
    assert ibge.sem_acento(None) == ""


# ---------------------------------------------------------------------
# Geração: reprodutibilidade
# ---------------------------------------------------------------------
def test_geracao_e_reprodutivel():
    """Mesma semente, mesmo conjunto. É o que permite conferir os números."""
    a = transacoes.gerar()
    linhas_a = (config.DADOS / "transacoes.csv").read_text(encoding="utf-8")
    b = transacoes.gerar()
    linhas_b = (config.DADOS / "transacoes.csv").read_text(encoding="utf-8")
    assert linhas_a == linhas_b
    assert a["linhas_no_csv"] == b["linhas_no_csv"] == 6383
    assert a["clientes"] == 400
    assert a["transacoes_limpas"] == 6198


def test_crm_tem_lacuna_proposital():
    """Nem todo comprador está no CRM, e o join tem de tratar o caso."""
    info = transacoes.gerar()
    assert info["cadastros_crm"] == 388
    assert len(info["sem_cadastro"]) == 12


def test_ordem_do_sorteio_nao_mudou():
    """
    Guarda contra uma regressão sutil: a ordem das chamadas ao gerador faz
    parte da semente. Trocar duas de lugar (mover o sorteio do perfil para
    dentro do dicionário, por exemplo) produz outro conjunto de dados sem
    quebrar nada visivelmente.
    """
    import random
    random.seed(config.SEMENTE_DADOS)
    c = transacoes.gerar_clientes(1)[0]
    assert c["nome_cliente"] == "Diego Rocha"
    assert c["idade"] == 59
    assert c["perfil"] == "perdido"
    assert c["corte"] == 7


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
