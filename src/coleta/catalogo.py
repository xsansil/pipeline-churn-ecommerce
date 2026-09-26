"""
Fonte 2 (JSON): catálogo de produtos.

Traz o que o extrato de vendas não tem: o **custo** de cada item e o
fornecedor. É por isso que essa fonte existe no pipeline: sem ela não há
como calcular margem, e a análise pararia na receita.

O arquivo é gravado em JSON porque é o formato em que catálogos costumam
circular entre sistemas, e porque o enunciado do projeto pede fontes
heterogêneas.
"""
import json

from .. import config
from .transacoes import PRODUTOS

# margem bruta por categoria: acessório gira mais barato e com margem maior
MARGEM = {
    "Acessorios": 0.55,
    "Perifericos": 0.42,
    "Audio": 0.38,
    "Armazenamento": 0.30,
    "Monitores": 0.22,
}

FORNECEDOR = {
    "Acessorios": "Conecta Distribuidora",
    "Perifericos": "TecnoSul Importacao",
    "Audio": "TecnoSul Importacao",
    "Armazenamento": "DataParts Brasil",
    "Monitores": "DataParts Brasil",
}


def gerar(destino=None):
    """Escreve o catálogo em JSON e devolve o caminho e a quantidade."""
    destino = (destino or config.DADOS) / "catalogo.json"

    itens = []
    for nome, categoria, preco in PRODUTOS:
        margem = MARGEM[categoria]
        itens.append({
            "produto": nome,
            "categoria": categoria,
            "preco_lista": round(preco, 2),
            "custo": round(preco * (1 - margem), 2),
            "fornecedor": FORNECEDOR[categoria],
        })

    destino.write_text(json.dumps(itens, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    return destino, len(itens)
