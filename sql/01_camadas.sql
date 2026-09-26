-- =====================================================================
-- Projeto Integrador: estrutura das camadas
-- Fundamentos de Banco de Dados (BDED-2026.1) · UNIFAP Digital
--
-- Arquitetura em camadas (Medallion), vista no Módulo 3:
--
--   bronze  o dado como chegou da fonte, sem tratamento, com a marca de
--           quando e de onde veio. É o que permite reprocessar tudo sem
--           voltar às fontes originais.
--   silver  o dado limpo, tipado e padronizado, já enriquecido.
--   gold    o dado pronto para consumo, uma linha por cliente, com as
--           features e o alvo. É daqui que o treino lê.
--   meta    proveniência: log de execuções, controle de carga incremental
--           e histórico de treinos.
--
-- Cada camada é um schema. Assim as permissões podem ser diferentes por
-- camada: quem consome o gold não precisa de acesso ao bronze.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS meta;

COMMENT ON SCHEMA bronze IS 'Dado cru, como veio das fontes, com metadados de ingestão';
COMMENT ON SCHEMA silver IS 'Dado limpo, tipado, padronizado e enriquecido';
COMMENT ON SCHEMA gold   IS 'Dado pronto para o modelo: uma linha por cliente';
COMMENT ON SCHEMA meta   IS 'Proveniência: execuções, controle incremental e treinos';


-- =====================================================================
-- META (precisa existir antes de tudo, porque as outras etapas gravam aqui)
-- =====================================================================

-- Log de execuções: cada passo de cada etapa vira uma linha. Nunca se
-- apaga nada aqui; é o histórico que responde "por que a tabela tem esse
-- número de linhas hoje".
CREATE TABLE IF NOT EXISTS meta.etl_execucoes (
    id          BIGSERIAL PRIMARY KEY,
    momento     TIMESTAMP   NOT NULL DEFAULT now(),
    etapa       TEXT        NOT NULL,
    fase        TEXT        NOT NULL,
    fonte       TEXT        NOT NULL,
    quantidade  INTEGER     NOT NULL,
    detalhe     TEXT,
    host        TEXT
);

CREATE INDEX IF NOT EXISTS ix_execucoes_momento ON meta.etl_execucoes (momento DESC);
CREATE INDEX IF NOT EXISTS ix_execucoes_etapa   ON meta.etl_execucoes (etapa, fase);

COMMENT ON TABLE meta.etl_execucoes IS
    'Histórico append-only de cada passo do pipeline';


-- Controle da carga incremental. A chave identifica a fonte; o valor é o
-- marco já processado (uma data, um id). UNIQUE na chave porque o UPSERT
-- do db.marca() depende dela.
CREATE TABLE IF NOT EXISTS meta.controle_extracao (
    chave         TEXT PRIMARY KEY,
    valor         TEXT NOT NULL,
    atualizado_em TIMESTAMP NOT NULL DEFAULT now()
);

COMMENT ON TABLE meta.controle_extracao IS
    'Marco da última extração por fonte: base da carga incremental';


-- Histórico de treinos: um registry rudimentar. Guarda as métricas de cada
-- modelo de cada execução, o que permite comparar versões ao longo do tempo
-- em vez de só olhar o treino mais recente.
CREATE TABLE IF NOT EXISTS meta.treinos (
    id              BIGSERIAL PRIMARY KEY,
    treinado_em     TIMESTAMP NOT NULL DEFAULT now(),
    execucao        UUID      NOT NULL,
    modelo          TEXT      NOT NULL,
    escolhido       BOOLEAN   NOT NULL DEFAULT FALSE,
    n_treino        INTEGER   NOT NULL,
    n_teste         INTEGER   NOT NULL,
    n_features      INTEGER   NOT NULL,
    corte_temporal  DATE      NOT NULL,
    acuracia        NUMERIC(6,4),
    precisao        NUMERIC(6,4),
    recall          NUMERIC(6,4),
    f1              NUMERIC(6,4),
    roc_auc         NUMERIC(6,4),
    cv_auc_medio    NUMERIC(6,4),
    cv_auc_desvio   NUMERIC(6,4),
    segundos        NUMERIC(8,2)
);

CREATE INDEX IF NOT EXISTS ix_treinos_execucao ON meta.treinos (execucao);
CREATE INDEX IF NOT EXISTS ix_treinos_auc      ON meta.treinos (roc_auc DESC);

COMMENT ON TABLE meta.treinos IS
    'Métricas de cada modelo de cada execução: registry para comparar versões';
COMMENT ON COLUMN meta.treinos.execucao IS
    'Agrupa os modelos treinados na mesma rodada';


-- =====================================================================
-- BRONZE: o dado como chegou
-- =====================================================================
-- Sem chave primária e sem restrição de domínio de propósito: a camada
-- bronze precisa aceitar o dado sujo. Datas e valores entram como TEXT
-- porque é assim que chegam, com vírgula decimal e "31/02/2024" no meio.

CREATE TABLE IF NOT EXISTS bronze.transacoes (
    transacao_id     TEXT,
    cliente_id       TEXT,
    nome_cliente     TEXT,
    email            TEXT,
    idade            TEXT,
    cidade           TEXT,
    estado           TEXT,
    data             TEXT,
    produto          TEXT,
    categoria        TEXT,
    quantidade       TEXT,
    valor            TEXT,
    canal            TEXT,
    forma_pagamento  TEXT,
    _ingerido_em     TIMESTAMP NOT NULL DEFAULT now(),
    _fonte           TEXT      NOT NULL DEFAULT 'csv'
);

CREATE TABLE IF NOT EXISTS bronze.catalogo (
    produto       TEXT,
    categoria     TEXT,
    preco_lista   TEXT,
    custo         TEXT,
    fornecedor    TEXT,
    _ingerido_em  TIMESTAMP NOT NULL DEFAULT now(),
    _fonte        TEXT      NOT NULL DEFAULT 'json'
);

CREATE TABLE IF NOT EXISTS bronze.municipios_ibge (
    municipio_id    TEXT,
    municipio       TEXT,
    uf              TEXT,
    mesorregiao     TEXT,
    microrregiao    TEXT,
    _ingerido_em    TIMESTAMP NOT NULL DEFAULT now(),
    _fonte          TEXT      NOT NULL DEFAULT 'api_ibge'
);

CREATE TABLE IF NOT EXISTS bronze.clientes (
    cliente_id      TEXT,
    nome            TEXT,
    email           TEXT,
    idade           TEXT,
    cidade          TEXT,
    estado          TEXT,
    cadastrado_em   TEXT,
    _ingerido_em    TIMESTAMP NOT NULL DEFAULT now(),
    _fonte          TEXT      NOT NULL DEFAULT 'postgresql'
);

COMMENT ON TABLE bronze.transacoes IS
    'Fonte 1 (CSV): transações cruas, com a sujeira preservada';
COMMENT ON TABLE bronze.catalogo IS
    'Fonte 2 (JSON): catálogo de produtos com custo e fornecedor';
COMMENT ON TABLE bronze.municipios_ibge IS
    'Fonte 3 (API REST): municípios do IBGE, coleta externa real';
COMMENT ON TABLE bronze.clientes IS
    'Fonte 4 (PostgreSQL): cadastro de clientes de banco existente';


-- =====================================================================
-- SILVER: o dado limpo e enriquecido
-- =====================================================================
-- Aqui já valem as restrições: os tipos são corretos e as regras de
-- negócio da limpeza viram CHECK. Se alguma linha inválida escapar da
-- limpeza em Python, o banco recusa. É defesa em profundidade.

CREATE TABLE IF NOT EXISTS silver.transacoes (
    transacao_id     BIGINT        NOT NULL,
    cliente_id       INTEGER       NOT NULL,
    nome_cliente     TEXT          NOT NULL,
    email            TEXT,
    email_valido     BOOLEAN       NOT NULL DEFAULT TRUE,
    idade            SMALLINT      NOT NULL CHECK (idade BETWEEN 0 AND 120),
    cidade           TEXT          NOT NULL,
    estado           CHAR(2)       NOT NULL,
    mesorregiao      TEXT,
    data             DATE          NOT NULL,
    produto          TEXT          NOT NULL,
    categoria        TEXT          NOT NULL,
    quantidade       SMALLINT      NOT NULL CHECK (quantidade BETWEEN 1 AND 20),
    valor            NUMERIC(12,2) NOT NULL CHECK (valor > 0),
    preco_unitario   NUMERIC(12,2) NOT NULL CHECK (preco_unitario > 0),
    custo            NUMERIC(12,2),
    margem           NUMERIC(12,2),
    canal            TEXT          NOT NULL,
    forma_pagamento  TEXT          NOT NULL,
    cliente_cadastrado BOOLEAN     NOT NULL,
    _processado_em   TIMESTAMP     NOT NULL DEFAULT now(),
    PRIMARY KEY (transacao_id)
);

CREATE INDEX IF NOT EXISTS ix_silver_cliente ON silver.transacoes (cliente_id);
CREATE INDEX IF NOT EXISTS ix_silver_data    ON silver.transacoes (data);
CREATE INDEX IF NOT EXISTS ix_silver_cli_dt  ON silver.transacoes (cliente_id, data);

COMMENT ON TABLE silver.transacoes IS
    'Transações limpas, tipadas e enriquecidas com IBGE e custo do catálogo';
COMMENT ON COLUMN silver.transacoes.mesorregiao IS
    'Veio da API do IBGE, casando pelo nome do município sem acento';
COMMENT ON COLUMN silver.transacoes.email_valido IS
    'E-mail malformado é marcado, não descartado: a venda aconteceu';
COMMENT ON COLUMN silver.transacoes.cliente_cadastrado IS
    'FALSE quando o comprador nao existe no CRM: os atributos vieram do CSV';


-- Dimensão de clientes: o cadastro do CRM, limpo. Fica separado das
-- transações porque a granularidade é outra: um cliente, uma linha. É
-- daqui que sai a data de cadastro, que vira feature no gold e que não
-- existiria sem a integração com o banco de origem.
CREATE TABLE IF NOT EXISTS silver.clientes (
    cliente_id     INTEGER   PRIMARY KEY,
    nome           TEXT      NOT NULL,
    email          TEXT      NOT NULL,
    idade          SMALLINT  NOT NULL CHECK (idade BETWEEN 18 AND 120),
    cidade         TEXT      NOT NULL,
    estado         CHAR(2)   NOT NULL,
    mesorregiao    TEXT,
    cadastrado_em  DATE      NOT NULL,
    _processado_em TIMESTAMP NOT NULL DEFAULT now()
);

COMMENT ON TABLE silver.clientes IS
    'Dimensao de clientes vinda do CRM: granularidade de um cliente por linha';


-- =====================================================================
-- GOLD: uma linha por cliente, pronta para o modelo
-- =====================================================================
-- A tabela de features é criada pelo Python (são 42 colunas, e a lista
-- muda se as features mudarem). O DDL aqui cuida do que é estável: as
-- previsões e as views de análise.

CREATE TABLE IF NOT EXISTS gold.previsoes (
    cliente_id       INTEGER   NOT NULL,
    execucao         UUID      NOT NULL,
    prevista_em      TIMESTAMP NOT NULL DEFAULT now(),
    probabilidade    NUMERIC(6,4) NOT NULL CHECK (probabilidade BETWEEN 0 AND 1),
    classificacao    TEXT      NOT NULL CHECK (classificacao IN ('churn', 'ativo')),
    faixa_de_risco   TEXT      NOT NULL,
    modelo           TEXT      NOT NULL,
    conjunto         TEXT      NOT NULL CHECK (conjunto IN ('treino', 'teste')),
    PRIMARY KEY (cliente_id, execucao)
);

CREATE INDEX IF NOT EXISTS ix_previsoes_prob ON gold.previsoes (probabilidade DESC);

COMMENT ON TABLE gold.previsoes IS
    'Saída do modelo por cliente: é o que a operação consome';
COMMENT ON COLUMN gold.previsoes.conjunto IS
    'De que lado da divisao o cliente ficou. Sem isso, qualquer avaliacao '
    'feita sobre esta tabela mistura dados que o modelo viu no treino com '
    'os que nao viu, e o resultado sai otimista.';
