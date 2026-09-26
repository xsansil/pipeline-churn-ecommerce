"""
Etapa 4. De gold para modelo: treino, comparação e persistência.

Lê a tabela de features do banco, compara cinco algoritmos, escolhe um e
grava três coisas:

    modelos/modelo_churn.pkl   o artefato para uso
    meta.treinos               as métricas de todos os modelos da rodada
    gold.previsoes             a probabilidade de cada cliente

O registro em `meta.treinos` é o que transforma o treino em histórico. Sem
ele só existe o último modelo, e não há como responder se a versão de hoje
é melhor do que a do mês passado. É uma pergunta que aparece assim que o
pipeline roda pela segunda vez.

## Sobre a métrica de seleção

A escolha usa ROC AUC, e não F1, por um motivo medido: neste conjunto a
linha de base (responder "churn" para todo mundo) obtém F1 de 0,718. Ela
acerta todos os positivos, então o recall é 1,0; e como 56% dos clientes
de fato dão churn, a precisão já nasce em 0,56. A média harmônica disso é
alta o bastante para superar a maioria dos modelos treinados, e selecionar
por F1 levaria à escolha errada. Em ROC AUC a mesma linha de base marca
exatamente 0,500, porque dá a mesma nota a todos e não ordena ninguém.
"""
import logging
import time
import uuid
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, f1_score, precision_score,
                             recall_score, roc_auc_score)
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

from .. import config, db, features

log = logging.getLogger("pipeline")

ARQUIVO = "modelo_churn.pkl"
LIMIAR = 0.5


def _modelos(semente):
    return {
        "Regressão Logística": LogisticRegression(max_iter=2000, random_state=semente),
        "Árvore de Decisão": DecisionTreeClassifier(max_depth=6, random_state=semente),
        "Random Forest": RandomForestClassifier(n_estimators=300, random_state=semente),
        "Gradient Boosting": GradientBoostingClassifier(n_estimators=200,
                                                        random_state=semente),
        "KNN (k=15)": KNeighborsClassifier(n_neighbors=15),
    }


def _metricas(y, pred, prob):
    return {
        "acuracia": accuracy_score(y, pred),
        "precisao": precision_score(y, pred, zero_division=0),
        "recall": recall_score(y, pred),
        "f1": f1_score(y, pred),
        "roc_auc": roc_auc_score(y, prob),
    }


def faixa(p):
    if p >= 0.75:
        return "alto"
    if p >= 0.50:
        return "médio"
    if p >= 0.25:
        return "baixo"
    return "muito baixo"


# ---------------------------------------------------------------------
def executar(eng, semente=None):
    db.inicia_execucao("modelo")
    semente = semente or config.SEMENTE
    execucao = uuid.uuid4()

    enc = db.le("select * from gold.features_clientes", eng)
    lista = features.lista_features(enc)
    num = features.numericas(lista)
    db.anota("LEITURA", "gold.features_clientes", len(enc),
             "%d features" % len(lista))

    X, y = enc[lista].copy(), enc["churn"].copy()

    # -----------------------------------------------------------------
    # Divisão estratificada e padronização
    # -----------------------------------------------------------------
    X_tr, X_te, y_tr, y_te, id_tr, id_te = train_test_split(
        X, y, enc["cliente_id"], test_size=0.25, random_state=semente, stratify=y)

    scaler = StandardScaler()
    X_tr_s, X_te_s = X_tr.copy(), X_te.copy()
    # fit SÓ no treino: ajustar no conjunto inteiro levaria a média e o
    # desvio do teste para dentro do treinamento (vazamento silencioso)
    X_tr_s[num] = scaler.fit_transform(X_tr[num])
    X_te_s[num] = scaler.transform(X_te[num])

    db.anota("DIVISAO", "treino / teste", len(X_tr),
             "%d treino (churn %.1f%%) · %d teste (churn %.1f%%) · %d colunas padronizadas"
             % (len(X_tr), 100 * y_tr.mean(), len(X_te), 100 * y_te.mean(), len(num)))

    # -----------------------------------------------------------------
    # Linha de base: o piso contra o qual os modelos são julgados
    # -----------------------------------------------------------------
    base = DummyClassifier(strategy="most_frequent").fit(X_tr_s, y_tr)
    m_base = _metricas(y_te, base.predict(X_te_s), base.predict_proba(X_te_s)[:, 1])
    db.anota("BASE", "sempre a classe majoritaria", 1,
             "auc=%.3f  f1=%.3f  acuracia=%.3f  <- o F1 alto e armadilha"
             % (m_base["roc_auc"], m_base["f1"], m_base["acuracia"]))

    # -----------------------------------------------------------------
    # Treino e comparação
    # -----------------------------------------------------------------
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=semente)
    linhas, treinados = [], {}

    for nome, modelo in _modelos(semente).items():
        t0 = time.time()
        cv_auc = cross_val_score(modelo, X_tr_s, y_tr, cv=cv, scoring="roc_auc")
        modelo.fit(X_tr_s, y_tr)
        prob = modelo.predict_proba(X_te_s)[:, 1]
        registro = {"modelo": nome}
        registro.update(_metricas(y_te, modelo.predict(X_te_s), prob))
        registro.update({
            "cv_auc_medio": cv_auc.mean(),
            "cv_auc_desvio": cv_auc.std(),
            "segundos": round(time.time() - t0, 2),
        })
        linhas.append(registro)
        treinados[nome] = modelo
        db.anota("TREINO", nome, int(round(1000 * registro["roc_auc"])),
                 "auc=%.3f  cv=%.3f±%.3f  f1=%.3f  (%.1fs)"
                 % (registro["roc_auc"], cv_auc.mean(), cv_auc.std(),
                    registro["f1"], registro["segundos"]))

    tabela = (pd.DataFrame(linhas).sort_values("roc_auc", ascending=False)
                .reset_index(drop=True))
    melhor = tabela.iloc[0]["modelo"]
    modelo_final = treinados[melhor]
    db.anota("SELECAO", melhor, int(round(1000 * tabela.iloc[0]["roc_auc"])),
             "escolhido por ROC AUC; por F1 seria %s"
             % tabela.sort_values("f1", ascending=False).iloc[0]["modelo"])

    # -----------------------------------------------------------------
    # Registro das métricas: o registry
    # -----------------------------------------------------------------
    registro = tabela.copy()
    registro.insert(0, "execucao", str(execucao))
    registro["escolhido"] = registro["modelo"] == melhor
    registro["n_treino"] = len(X_tr)
    registro["n_teste"] = len(X_te)
    registro["n_features"] = len(lista)
    registro["corte_temporal"] = pd.Timestamp(config.CORTE).date()
    registro["treinado_em"] = datetime.now()
    registro = registro[["treinado_em", "execucao", "modelo", "escolhido",
                         "n_treino", "n_teste", "n_features", "corte_temporal",
                         "acuracia", "precisao", "recall", "f1", "roc_auc",
                         "cv_auc_medio", "cv_auc_desvio", "segundos"]]
    db.grava(registro, "treinos", "meta", eng, "acrescentar")

    # -----------------------------------------------------------------
    # Importância: corrigida pela escala
    # -----------------------------------------------------------------
    influentes = _influencia(modelo_final, X_tr_s, lista)
    if influentes is not None:
        topo = influentes.head(5)
        db.anota("ANALISE", "features mais influentes", len(influentes),
                 " · ".join("%s %+.2f" % (i, v) for i, v in topo.items()))

    # -----------------------------------------------------------------
    # Previsão para a carteira inteira
    # -----------------------------------------------------------------
    X_todos = X.copy()
    X_todos[num] = scaler.transform(X[num])
    prob_todos = modelo_final.predict_proba(X_todos)[:, 1]

    # marcar de que lado da divisão cada cliente ficou é o que permite
    # avaliar a tabela depois sem misturar o que o modelo viu no treino
    conjunto = np.where(enc["cliente_id"].isin(set(id_te)), "teste", "treino")

    previsoes = pd.DataFrame({
        "cliente_id": enc["cliente_id"],
        "execucao": str(execucao),
        "probabilidade": prob_todos.round(4),
        "classificacao": np.where(prob_todos >= LIMIAR, "churn", "ativo"),
        "faixa_de_risco": [faixa(p) for p in prob_todos],
        "modelo": melhor,
        "conjunto": conjunto,
    })
    db.grava(previsoes, "previsoes", "gold", eng, "acrescentar")
    db.anota("PREVISAO", "gold.previsoes", len(previsoes),
             "%d em risco alto · %d treino / %d teste"
             % (int((previsoes.faixa_de_risco == "alto").sum()),
                int((conjunto == "treino").sum()), int((conjunto == "teste").sum())))

    # -----------------------------------------------------------------
    # Persistência: um artefato só
    # -----------------------------------------------------------------
    # O scaler vai dentro do mesmo arquivo de propósito. Como artefato
    # separado ele é a peça que se esquece: o modelo aceita valores crus
    # sem reclamar e devolve probabilidade sem sentido. Juntos, não há como
    # carregar um sem o outro.
    caminho = config.MODELOS / ARQUIVO
    joblib.dump({
        "modelo": modelo_final,
        "scaler": scaler,
        "nome": melhor,
        "features": lista,              # a ordem das colunas é parte do contrato
        "numericas": num,               # quais o scaler transforma
        "limiar": LIMIAR,
        "metricas": tabela[tabela.modelo == melhor].iloc[0].to_dict(),
        "linha_de_base": m_base,
        "execucao": str(execucao),
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "corte_temporal": config.CORTE,
        "semente": semente,
    }, caminho)
    db.anota("ARTEFATO", caminho.name, int(caminho.stat().st_size / 1024),
             "KB · modelo + scaler + ordem das features")

    db.grava_execucao(eng)
    return {"execucao": str(execucao), "melhor": melhor, "tabela": tabela,
            "base": m_base, "caminho": caminho}


def _influencia(modelo, X_treino, lista):
    """
    Ranking de influência comparável entre colunas de escalas diferentes.

    O coeficiente cru não serve: as numéricas foram padronizadas (desvio
    1,0) e as binárias do One-Hot não (desvio entre 0,2 e 0,5, porque cada
    categoria vale 1 em poucas linhas). Para o mesmo efeito no resultado, a
    coluna esparsa precisa de um coeficiente várias vezes maior, e sobe no
    ranking sem ter mais influência. Multiplicar pelo desvio da própria
    coluna corrige isso.
    """
    if hasattr(modelo, "coef_"):
        valores = pd.Series(modelo.coef_[0], index=lista) * X_treino[lista].std()
    elif hasattr(modelo, "feature_importances_"):
        valores = pd.Series(modelo.feature_importances_, index=lista)
    else:
        return None
    return valores.reindex(valores.abs().sort_values(ascending=False).index)


def criar_views(eng):
    """
    Aplica as views de consumo. Roda depois do treino porque elas dependem
    de gold.previsoes e de meta.treinos, que só existem a partir dele.
    """
    db.inicia_execucao("views")
    db.executa_sql(eng, config.SQL / "02_views.sql")
    with eng.connect() as c:
        from sqlalchemy import text as _t
        n = c.execute(_t("""
            select count(*) from information_schema.views
             where table_schema in ('gold','meta')""")).scalar()
    db.anota("VIEWS", "views de consumo", n, "gold e meta")
    db.grava_execucao(eng)
    return n
