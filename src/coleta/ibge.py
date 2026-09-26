"""
Fonte 3 (API REST) — municípios do IBGE.

A única fonte externa de verdade do projeto: uma API pública, fora do meu
controle, que pode estar lenta ou fora do ar na hora em que o pipeline
rodar. É o que torna esta etapa interessante — e o que obriga a tratar
falha de rede como caso normal, não como exceção.

Duas defesas:

  * **cache em disco.** A primeira coleta grava o resultado. Se a API não
    responder na próxima execução, o pipeline usa o cache e segue, anotando
    no log que foi por ele. Município não muda de mesorregião com
    frequência; dado de ontem serve.
  * **degradação parcial.** A coleta é por UF. Se uma falhar, as outras
    entram assim mesmo — o enriquecimento fica incompleto para aquele
    estado, e isso é melhor do que derrubar o pipeline inteiro.

O casamento com a cidade da transação é pelo nome **sem acento e em caixa
baixa**: o IBGE devolve "Macapá" e o extrato traz "MACAPA", "  macapa" ou
"Macapa". Sem normalizar, o join casa quase nada e não reclama.
"""
import json
import logging
import unicodedata

import requests

from .. import config

log = logging.getLogger("pipeline")


def sem_acento(s):
    """'Macapá' -> 'macapa'. Chave de junção entre o IBGE e as transações."""
    if s is None:
        return ""
    normal = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in normal if not unicodedata.combining(c)).strip().lower()


def _do_cache(caminho):
    if not caminho.exists():
        return None
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("cache do IBGE ilegível (%s); será ignorado", e)
        return None


def coletar(ufs=None, timeout=20, destino=None):
    """
    Busca os municípios das UFs na API do IBGE.

    Devolve (registros, origem), em que origem é 'api', 'api+cache' ou
    'cache' — a etapa de carga registra isso no log de proveniência, para
    que depois se saiba se aquele dado veio da rede ou do disco.
    """
    ufs = ufs or config.IBGE_UFS
    cache = (destino or config.DADOS) / "municipios_ibge.json"

    registros, falharam = [], []
    for uf in ufs:
        try:
            r = requests.get(config.IBGE_URL.format(uf), timeout=timeout)
            r.raise_for_status()
            for m in r.json():
                # a resposta é aninhada: municipio > microrregiao > mesorregiao
                micro = m.get("microrregiao") or {}
                meso = micro.get("mesorregiao") or {}
                registros.append({
                    "municipio_id": str(m["id"]),
                    "municipio": m["nome"],
                    "municipio_chave": sem_acento(m["nome"]),
                    "uf": uf,
                    "mesorregiao": meso.get("nome"),
                    "microrregiao": micro.get("nome"),
                })
        except (requests.RequestException, ValueError, KeyError) as e:
            falharam.append(uf)
            log.warning("IBGE %s indisponível (%s)", uf, type(e).__name__)

    if not registros:
        guardado = _do_cache(cache)
        if guardado:
            log.warning("nenhuma UF respondeu; usando o cache de %s", cache.name)
            return guardado, "cache"
        raise RuntimeError(
            "A API do IBGE não respondeu e não há cache em %s. "
            "Rode novamente com a rede disponível." % cache)

    if falharam:
        # completa o que faltou com o que já estava em disco
        guardado = _do_cache(cache) or []
        ja_tem = {r["municipio_id"] for r in registros}
        recuperados = [r for r in guardado
                       if r["uf"] in falharam and r["municipio_id"] not in ja_tem]
        if recuperados:
            log.warning("completando %s com %d municípios do cache",
                        "/".join(falharam), len(recuperados))
            registros.extend(recuperados)
        origem = "api+cache"
    else:
        origem = "api"

    cache.write_text(json.dumps(registros, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return registros, origem
