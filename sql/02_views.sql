-- =====================================================================
-- Projeto Integrador: views de consumo
-- Fundamentos de Banco de Dados (BDED-2026.1) · UNIFAP Digital
--
-- As views são a interface do gold. A API e qualquer ferramenta de BI leem
-- daqui, não das tabelas: assim a forma de consumo fica estável mesmo que
-- a estrutura interna mude.
--
-- Todas filtram pela execução mais recente. `gold.previsoes` e
-- `meta.treinos` são append-only (guardam todas as rodadas), e sem o
-- filtro as consultas somariam previsões de treinos diferentes.
-- =====================================================================

-- Execução mais recente, usada como filtro pelas demais views.
CREATE OR REPLACE VIEW gold.vw_ultima_execucao AS
SELECT execucao, treinado_em, modelo, roc_auc, n_treino, n_teste
  FROM meta.treinos
 WHERE escolhido
 ORDER BY treinado_em DESC
 LIMIT 1;

COMMENT ON VIEW gold.vw_ultima_execucao IS
    'O treino vigente: as outras views se ancoram nele';


-- =====================================================================
-- Carteira ordenada por risco
-- =====================================================================
-- É a entrega operacional do projeto: a lista que a equipe de retenção
-- usa. O valor de um modelo com AUC 0,73 não é acertar quem sai, é
-- ORDENAR a carteira para que se comece pelo topo.
CREATE OR REPLACE VIEW gold.vw_clientes_risco AS
SELECT
    p.cliente_id,
    COALESCE(c.nome, '(sem cadastro no CRM)')                   AS nome,
    COALESCE(c.cidade, 'Nao informada')                         AS cidade,
    c.mesorregiao,
    p.conjunto,
    p.probabilidade,
    p.faixa_de_risco,
    -- posição na fila de abordagem
    ROW_NUMBER() OVER (ORDER BY p.probabilidade DESC)          AS posicao,
    -- decil de risco: 1 = os 10% mais prováveis de sair
    NTILE(10)    OVER (ORDER BY p.probabilidade DESC)          AS decil,
    f.total_gasto,
    f.margem_total,
    f.qtd_compras,
    f.recencia_dias,
    f.razao_recencia_intervalo,
    f.churn                                                     AS churn_real,
    (p.classificacao = 'churn') = (f.churn = 1)                 AS acertou
  FROM gold.previsoes p
  JOIN gold.features_clientes f USING (cliente_id)
  LEFT JOIN silver.clientes   c USING (cliente_id)
 WHERE p.execucao = (SELECT execucao FROM gold.vw_ultima_execucao);

COMMENT ON VIEW gold.vw_clientes_risco IS
    'Carteira ordenada pela probabilidade de churn: a lista de abordagem';


-- =====================================================================
-- Ganho por decil: o modelo ordena a carteira?
-- =====================================================================
-- A leitura honesta desta tabela exige separar treino de teste. O modelo
-- viu 299 dos 399 clientes durante o treinamento, e a concentracao de
-- churn nos primeiros decis fica otimista se os dois forem somados. O
-- numero que vale para decidir e o da coluna 'teste'.
CREATE OR REPLACE VIEW gold.vw_ganho_por_decil AS
SELECT
    decil,
    COUNT(*)                                                   AS clientes,
    COUNT(*) FILTER (WHERE conjunto = 'teste')                 AS no_teste,
    ROUND(100.0 * SUM(churn_real) / COUNT(*), 1)               AS pct_churn_geral,
    ROUND(100.0 * SUM(churn_real) FILTER (WHERE conjunto = 'teste')
          / NULLIF(COUNT(*) FILTER (WHERE conjunto = 'teste'), 0), 1)
                                                               AS pct_churn_teste,
    ROUND(AVG(probabilidade), 3)                               AS risco_medio
  FROM gold.vw_clientes_risco
 GROUP BY decil
 ORDER BY decil;

COMMENT ON VIEW gold.vw_ganho_por_decil IS
    'Concentracao de churn por decil de risco: separando treino de teste';


-- =====================================================================
-- Receita sob risco, por mesorregião
-- =====================================================================
-- Só existe por causa da API do IBGE: a mesorregião não vem no extrato de
-- vendas. Cruza o risco previsto com o valor que o cliente representa.
CREATE OR REPLACE VIEW gold.vw_risco_por_regiao AS
SELECT
    COALESCE(r.mesorregiao, 'Sem região')            AS mesorregiao,
    COUNT(*)                                          AS clientes,
    COUNT(*) FILTER (WHERE r.faixa_de_risco = 'alto') AS risco_alto,
    ROUND(AVG(r.probabilidade), 3)                    AS risco_medio,
    ROUND(SUM(r.total_gasto)::numeric, 2)             AS receita_historica,
    -- quanto de receita está associada a quem tem risco alto
    ROUND(SUM(r.total_gasto) FILTER
          (WHERE r.faixa_de_risco = 'alto')::numeric, 2) AS receita_em_risco,
    ROUND(100.0 * COUNT(*) FILTER (WHERE r.faixa_de_risco = 'alto')
          / COUNT(*), 1)                              AS pct_risco_alto
  FROM gold.vw_clientes_risco r
 GROUP BY 1;

COMMENT ON VIEW gold.vw_risco_por_regiao IS
    'Risco e receita por mesorregiao: depende do enriquecimento do IBGE';


-- =====================================================================
-- Comparação dos modelos
-- =====================================================================
-- Lê o registry. Mostra o desempenho por modelo ao longo das rodadas, que
-- é a pergunta que aparece assim que o pipeline roda pela segunda vez.
CREATE OR REPLACE VIEW gold.vw_desempenho_modelos AS
SELECT
    modelo,
    COUNT(*)                                  AS rodadas,
    ROUND(AVG(roc_auc), 4)                    AS auc_medio,
    ROUND(AVG(cv_auc_medio), 4)               AS cv_auc_medio,
    ROUND(AVG(f1), 4)                         AS f1_medio,
    ROUND(AVG(acuracia), 4)                   AS acuracia_media,
    COUNT(*) FILTER (WHERE escolhido)         AS vezes_escolhido,
    ROUND(AVG(segundos), 2)                   AS segundos_medio,
    MAX(treinado_em)                          AS ultimo_treino
  FROM meta.treinos
 GROUP BY modelo
 ORDER BY auc_medio DESC;

COMMENT ON VIEW gold.vw_desempenho_modelos IS
    'Desempenho por algoritmo ao longo das rodadas: comparacao de versoes';


-- =====================================================================
-- O que cada fonte acrescentou
-- =====================================================================
-- Responde com SQL a pergunta que justifica a arquitetura multi-fonte:
-- quantas features vieram de cada origem, e o quanto elas se movem com o
-- alvo. A resposta honesta, nesta base, é que a integração melhorou muito
-- a qualidade do dado e pouco o poder preditivo.
CREATE OR REPLACE VIEW gold.vw_contribuicao_das_fontes AS
SELECT
    origem,
    COUNT(*)                                        AS features,
    ROUND(MAX(ABS(corr_com_churn))::numeric, 3)     AS maior_correlacao,
    ROUND(AVG(ABS(corr_com_churn))::numeric, 3)     AS correlacao_media,
    COUNT(*) FILTER (WHERE ABS(corr_com_churn) > 0.30) AS features_relevantes
  FROM gold.features_dicionario
 GROUP BY origem
 ORDER BY maior_correlacao DESC;

COMMENT ON VIEW gold.vw_contribuicao_das_fontes IS
    'Quantas features cada fonte gerou e o quanto elas se movem com o alvo';


-- =====================================================================
-- Resumo da última execução do ETL
-- =====================================================================
CREATE OR REPLACE VIEW meta.vw_resumo_execucao AS
WITH ultima AS (
    SELECT etapa, MAX(momento) AS fim
      FROM meta.etl_execucoes
     GROUP BY etapa
)
SELECT
    e.etapa,
    u.fim                                          AS concluida_em,
    COUNT(*)                                       AS passos,
    STRING_AGG(DISTINCT e.fase, ', ' ORDER BY e.fase) AS fases
  FROM meta.etl_execucoes e
  JOIN ultima u ON u.etapa = e.etapa
                AND e.momento > u.fim - INTERVAL '5 minutes'
 GROUP BY e.etapa, u.fim
 ORDER BY u.fim;

COMMENT ON VIEW meta.vw_resumo_execucao IS
    'Resumo da ultima passagem de cada etapa do pipeline';
