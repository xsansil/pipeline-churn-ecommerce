"""
Fonte 1 (CSV) e insumo da Fonte 4 (CRM) — gerador do dado bruto.

Simula duas origens que existem separadas na vida real:

  * o **extrato de transações** que a plataforma de e-commerce exporta em
    CSV — desnormalizado (repete nome, e-mail, cidade em toda linha) e
    sujo, porque é um dump operacional;
  * o **cadastro de clientes** do CRM, que é a fonte autoritativa desses
    mesmos atributos e está limpa.

A duplicação entre as duas é proposital: é ela que dá ao pipeline uma
decisão real de integração — de qual lado vem o nome do cliente quando os
dois discordam.

Cada cliente recebe um PERFIL DE ATIVIDADE oculto, que rege a chance de
comprar em cada mês. É esse perfil que cria o sinal de churn que o modelo
tem de inferir; ele não vai para nenhum arquivo.

    fiel      compra de forma estável até o fim do período
    declinio  a frequência cai mês a mês
    perdido   para de comprar em algum ponto e não volta

Só biblioteca padrão.
"""
import csv
import json
import random
from datetime import date, timedelta

from .. import config

MESES = 24                       # 2023-01 até 2024-12

PRODUTOS = [
    ("Cabo HDMI 2m",           "Acessorios",     35.00),
    ("Mousepad grande",        "Acessorios",     45.00),
    ("Suporte para notebook",  "Acessorios",     85.00),
    ("Hub USB-C 7 portas",     "Acessorios",    120.00),
    ("Mouse sem fio",          "Perifericos",    90.00),
    ("Teclado mecanico",       "Perifericos",   250.00),
    ("Webcam Full HD",         "Perifericos",   350.00),
    ("Caixa de som bluetooth", "Audio",         180.00),
    ("Headset gamer",          "Audio",         450.00),
    ("Pen drive 64GB",         "Armazenamento",  55.00),
    ("SSD 1TB",                "Armazenamento", 480.00),
    ("Monitor 24 polegadas",   "Monitores",     900.00),
]

CIDADES = [("Macapa", "AP"), ("Santana", "AP"), ("Oiapoque", "AP"),
           ("Laranjal do Jari", "AP"), ("Belem", "PA"), ("Santarem", "PA"),
           ("Maraba", "PA"), ("Manaus", "AM"), ("Parintins", "AM"),
           ("Boa Vista", "RR")]

CANAIS = ["Site", "Site", "Aplicativo", "WhatsApp", "Loja fisica"]
PAGAMENTOS = ["Pix", "Pix", "Cartao de credito", "Cartao de debito", "Boleto"]

PRENOMES = ["Ana", "Bruno", "Carla", "Diego", "Elaine", "Fabio", "Gabriela",
            "Heitor", "Isabela", "Joao", "Karina", "Lucas", "Marina", "Nathan",
            "Otavio", "Patricia", "Rafael", "Simone", "Tiago", "Vanessa",
            "Wagner", "Yara", "Bruna", "Caio", "Denise", "Eduardo", "Fernanda",
            "Gustavo", "Helena", "Igor"]
SOBRENOMES = ["Silva", "Santos", "Oliveira", "Costa", "Ferreira", "Almeida",
              "Souza", "Gomes", "Martins", "Tavares", "Rocha", "Ramalho",
              "Coutinho", "Menezes", "Pinheiro", "Barbosa", "Freitas",
              "Vasques", "Andrade", "Lopes", "Duarte", "Nunes", "Cardoso",
              "Moreira", "Bezerra"]

CAMPOS = ["transacao_id", "cliente_id", "nome_cliente", "email", "idade",
          "cidade", "estado", "data", "produto", "categoria",
          "quantidade", "valor", "canal", "forma_pagamento"]


def _primeiro_dia(m):
    return date(2023 + m // 12, m % 12 + 1, 1)


def _dia_do_mes(m):
    base = _primeiro_dia(m)
    prox = _primeiro_dia(m + 1) if m < MESES - 1 else date(2025, 1, 1)
    return base + timedelta(days=random.randint(0, (prox - base).days - 1))


def _probabilidade(perfil, mes, corte):
    """Chance de o cliente comprar neste mês, conforme o perfil oculto."""
    if perfil == "fiel":
        return 0.62
    if perfil == "declinio":
        return max(0.05, 0.70 - 0.026 * mes)
    return 0.60 if mes < corte else 0.015          # perdido


# ---------------------------------------------------------------------
def gerar_clientes(n):
    usados, clientes = set(), []
    for i in range(1, n + 1):
        while True:
            nome = "%s %s" % (random.choice(PRENOMES), random.choice(SOBRENOMES))
            if nome not in usados:
                usados.add(nome)
                break
        cidade, uf = random.choice(CIDADES)
        # O perfil é sorteado aqui, antes do dicionário, e não dentro dele.
        # A ordem das chamadas ao gerador faz parte da semente: trocar duas
        # delas de lugar produz um conjunto de dados inteiramente diferente,
        # ainda que o código pareça equivalente. Manter esta ordem é o que
        # reproduz o dataset usado nos laboratórios P12 a P14.
        perfil = random.choices(["fiel", "declinio", "perdido"],
                                weights=[30, 35, 35])[0]
        clientes.append({
            "cliente_id": i,
            "nome_cliente": nome,
            "email": "%s@email.com" % nome.lower().replace(" ", "."),
            "idade": random.randint(18, 72),
            "cidade": cidade,
            "estado": uf,
            "perfil": perfil,
            "corte": random.randint(4, 20),    # mês em que o "perdido" some
        })
    return clientes


def gerar_transacoes(clientes):
    linhas, tid = [], 0
    for c in clientes:
        for m in range(MESES):
            if random.random() >= _probabilidade(c["perfil"], m, c["corte"]):
                continue
            for _ in range(random.choices([1, 2, 3], weights=[65, 25, 10])[0]):
                tid += 1
                nome, categoria, preco = random.choice(PRODUTOS)
                qtd = random.choices([1, 2, 3, 4], weights=[50, 28, 15, 7])[0]
                linhas.append({
                    "transacao_id": tid,
                    "cliente_id": c["cliente_id"],
                    "nome_cliente": c["nome_cliente"],
                    "email": c["email"],
                    "idade": c["idade"],
                    "cidade": c["cidade"],
                    "estado": c["estado"],
                    "data": _dia_do_mes(m).isoformat(),
                    "produto": nome,
                    "categoria": categoria,
                    "quantidade": qtd,
                    "valor": round(preco * qtd, 2),
                    "canal": random.choice(CANAIS),
                    "forma_pagamento": random.choice(PAGAMENTOS),
                })
    return linhas


def sujar(linhas):
    """Injeta os onze problemas. Devolve o relatório do que foi feito."""
    n = len(linhas)
    rel = []

    def amostra(k):
        return random.sample(range(n), k)

    k = int(n * 0.05)
    for i in amostra(k):
        linhas[i]["idade"] = ""
    rel.append(("idade ausente", k))

    k = int(n * 0.03)
    for i in amostra(k):
        linhas[i]["cidade"] = ""
    rel.append(("cidade ausente", k))

    k = int(n * 0.02)
    for i in amostra(k):
        linhas[i]["valor"] = ""
    rel.append(("valor ausente", k))

    k = int(n * 0.015)
    for i in amostra(k):
        linhas[i]["data"] = random.choice(["", "31/02/2024", "data invalida"])
    rel.append(("data ausente ou invalida", k))

    k = int(n * 0.012)
    for i in amostra(k):
        linhas[i]["valor"] = round(float(linhas[i]["valor"] or 100) * 1000, 2)
    rel.append(("valor absurdo (erro de digitacao)", k))

    k = int(n * 0.01)
    for i in amostra(k):
        v = linhas[i]["valor"]
        if v:
            linhas[i]["valor"] = -abs(float(v))
    rel.append(("valor negativo", k))

    k = int(n * 0.008)
    for i in amostra(k):
        linhas[i]["quantidade"] = random.choice([0, -1, 999])
    rel.append(("quantidade invalida", k))

    k = int(n * 0.06)
    for i in amostra(k):
        if linhas[i]["cidade"]:
            linhas[i]["cidade"] = random.choice([
                linhas[i]["cidade"].upper(),
                "  " + linhas[i]["cidade"],
                linhas[i]["cidade"].lower() + " "])
        linhas[i]["produto"] = random.choice([
            linhas[i]["produto"].upper(), linhas[i]["produto"] + "  "])
    rel.append(("texto com espaco ou caixa inconsistente", k))

    k = int(n * 0.02)
    for i in amostra(k):
        linhas[i]["email"] = random.choice([
            linhas[i]["email"].replace("@", ""),
            linhas[i]["email"].upper(),
            "  " + linhas[i]["email"]])
    rel.append(("email malformado", k))

    k = int(n * 0.015)
    for i in amostra(k):
        v = linhas[i]["valor"]
        if v not in ("", None):
            linhas[i]["valor"] = ("%.2f" % float(v)).replace(".", ",")
    rel.append(("valor em formato brasileiro", k))

    k = int(n * 0.03)
    for i in random.sample(range(n), k):
        linhas.append(dict(linhas[i]))
    rel.append(("duplicata exata", k))

    random.shuffle(linhas)
    return rel


def cadastro_crm(clientes, ausentes=12):
    """
    O recorte que o CRM conhece: dados limpos, mas nem todo cliente está lá.

    Alguns compraram sem completar o cadastro. A lacuna é deliberada: é o
    que obriga o pipeline a decidir o que fazer quando o join não casa, em
    vez de deixar a falha passar em silêncio.
    """
    sorteio = random.Random(7)          # sequência própria, não perturba a principal
    fora = set(sorteio.sample([c["cliente_id"] for c in clientes], ausentes))
    base = date(2022, 6, 1)
    registros = []
    for c in clientes:
        if c["cliente_id"] in fora:
            continue
        registros.append({
            "cliente_id": c["cliente_id"],
            "nome": c["nome_cliente"],
            "email": c["email"],
            "idade": c["idade"],
            "cidade": c["cidade"],
            "estado": c["estado"],
            "cadastrado_em": (base + timedelta(
                days=sorteio.randint(0, 300))).isoformat(),
        })
    return registros, sorted(fora)


# ---------------------------------------------------------------------
def gerar(n_clientes=None, destino=None):
    """Produz o CSV sujo e o cadastro do CRM. Devolve um resumo."""
    random.seed(config.SEMENTE_DADOS)
    destino = destino or config.DADOS
    n_clientes = n_clientes or config.N_CLIENTES

    clientes = gerar_clientes(n_clientes)
    linhas = gerar_transacoes(clientes)
    limpas = len(linhas)
    relatorio = sujar(linhas)
    crm, sem_cadastro = cadastro_crm(clientes)

    csv_path = destino / "transacoes.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        w.writerows(linhas)

    crm_path = destino / "clientes_crm.json"
    crm_path.write_text(json.dumps(crm, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    perfis = {}
    for c in clientes:
        perfis[c["perfil"]] = perfis.get(c["perfil"], 0) + 1

    return {
        "csv": csv_path,
        "crm": crm_path,
        "clientes": len(clientes),
        "transacoes_limpas": limpas,
        "linhas_no_csv": len(linhas),
        "cadastros_crm": len(crm),
        "sem_cadastro": sem_cadastro,
        "perfis": perfis,
        "sujeira": relatorio,
    }
