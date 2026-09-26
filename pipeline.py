"""
Projeto Integrador — orquestrador do pipeline.

Fundamentos de Banco de Dados (BDED-2026.1) · UNIFAP Digital
Aluno: Sandro

    python pipeline.py                 roda tudo, do zero ao modelo
    python pipeline.py --etapa bronze  roda só uma etapa
    python pipeline.py --recriar       descarta as camadas e refaz

    fontes → bronze → silver → gold → modelo
"""
import argparse
import logging
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from src import config                                    # noqa: E402
from src.etl import bronze, gold, preparar, silver        # noqa: E402
from src.modelo import treinar                            # noqa: E402

ETAPAS = ["preparar", "bronze", "silver", "gold", "modelo"]


def configura_log(verboso=False):
    fmt = "%(asctime)s  %(levelname)-7s %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format=fmt, datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOGS / "pipeline.log", encoding="utf-8"),
        ])
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    return logging.getLogger("pipeline")


def cabecalho(n, titulo):
    print()
    print("=" * 70)
    print("ETAPA %s — %s" % (n, titulo))
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(description="Pipeline de churn — projeto integrador")
    ap.add_argument("--etapa", choices=ETAPAS, help="roda apenas esta etapa")
    ap.add_argument("--recriar", action="store_true",
                    help="descarta as camadas antes de recriar")
    ap.add_argument("-v", "--verboso", action="store_true")
    args = ap.parse_args()

    log = configura_log(args.verboso)
    inicio = time.time()

    print("PIPELINE DE CHURN — e-commerce de informática")
    print("fontes → bronze → silver → gold → modelo")

    alvo = [args.etapa] if args.etapa else ETAPAS

    eng = None
    if "preparar" in alvo:
        cabecalho(0, "PREPARAR O BANCO")
        eng = preparar.executar(recriar=args.recriar)

    if "bronze" in alvo:
        cabecalho(1, "COLETA DAS FONTES → BRONZE")
        eng = eng or preparar.executar()
        bronze.executar(eng, recriar=args.recriar)

    if "silver" in alvo:
        cabecalho(2, "LIMPEZA E INTEGRAÇÃO → SILVER")
        eng = eng or preparar.executar()
        silver.executar(eng)

    if "gold" in alvo:
        cabecalho(3, "ENGENHARIA DE FEATURES → GOLD")
        eng = eng or preparar.executar()
        gold.executar(eng)

    if "modelo" in alvo:
        cabecalho(4, "TREINO E SELEÇÃO DO MODELO")
        eng = eng or preparar.executar()
        treinar.executar(eng)

    print()
    print("=" * 70)
    print("CONCLUÍDO em %.1f segundos" % (time.time() - inicio))
    print("=" * 70)
    log.debug("fim")


if __name__ == "__main__":
    main()
