"""
Etapa 0 — prepara o banco: cria o database e aplica o DDL das camadas.

É idempotente: pode rodar quantas vezes for preciso. O DDL usa
CREATE ... IF NOT EXISTS em tudo, então reexecutar não apaga dado.
"""
import logging

from sqlalchemy import text

from .. import config, db

log = logging.getLogger("pipeline")


def executar():
    db.inicia_execucao("preparar")

    eng = db.engine(criar_se_faltar=True)
    db.anota("SETUP", "banco " + config.PG_BANCO, 1, config.url_segura())

    db.executa_sql(eng, config.SQL / "01_camadas.sql")

    with eng.connect() as c:
        schemas = [r[0] for r in c.execute(text("""
            select nspname from pg_namespace
             where nspname = any(:c) order by 1"""), {"c": config.CAMADAS})]
        tabelas = c.execute(text("""
            select count(*) from information_schema.tables
             where table_schema = any(:c)"""), {"c": config.CAMADAS}).scalar()

    db.anota("SETUP", "schemas", len(schemas), ", ".join(schemas))
    db.anota("SETUP", "tabelas", tabelas, "nas quatro camadas")

    faltando = set(config.CAMADAS) - set(schemas)
    if faltando:
        raise RuntimeError("schemas não criados: %s" % sorted(faltando))

    db.grava_execucao(eng)
    return eng
