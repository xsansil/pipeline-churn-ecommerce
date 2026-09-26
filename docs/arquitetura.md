# Arquitetura do pipeline

Documento de referência do fluxo de dados. O [README](../README.md) tem a visão
geral; aqui está o detalhe tabela a tabela.

---

## 1. Fluxo completo

```
 FONTES                   EXTRAÇÃO           BRONZE                  SILVER                 GOLD                    CONSUMO
 ──────────────────────   ────────────       ───────────────────     ──────────────────     ───────────────────     ─────────────

 ┌────────────────────┐
 │ 1  CSV             │                      bronze.transacoes
 │ transacoes.csv     │───────┐              6.383 linhas, TEXT
 │ 6.383 linhas sujas │       │              _ingerido_em, _fonte
 └────────────────────┘       │                      │
                              │                      │  limpeza em 7 etapas
 ┌────────────────────┐       │                      │  (6.383 → 5.809)
 │ 2  JSON            │       │              bronze.catalogo         silver.transacoes
 │ catalogo.json      │───────┤              12 produtos      ─────▶ 5.809 transações
 │ custo, fornecedor  │       │              custo unitário          PK, CHECK, 3 índices
 └────────────────────┘       │                      │               custo, margem
                              ├─── extract ──┤                       mesorregião
 ┌────────────────────┐       │                      │               cliente_cadastrado
 │ 3  API REST        │       │              bronze.municipios_ibge          │
 │ IBGE localidades   │───────┤              237 municípios                  │
 │ cache + 3 níveis   │       │              AP, PA, AM, RR                  │
 │ de degradação      │       │                      │               silver.clientes
 └────────────────────┘       │                      │        ─────▶ 388 cadastros
                              │                      │               dimensão, PK
 ┌────────────────────┐       │              bronze.clientes                 │
 │ 4  PostgreSQL      │───────┘              388 cadastros                   │
 │ crm_origem         │                      carga incremental               │
 │ 388 de 400         │                                                      │
 └────────────────────┘                                                      │
                                                        separação temporal   │
                                                        corte 2024-10-01     │
                                                                             ▼
                                                                   gold.features_clientes
                                                                   399 clientes × 44 features
                                                                   + alvo churn
                                                                             │
                                                                             │  treino de 5 modelos
                                                                             ▼
                                        meta.treinos ◀──────────── modelo_churn.pkl      ┌──────────────┐
                                        5 linhas por rodada        modelo + scaler  ────▶│ 7 views      │
                                                                                          │ API FastAPI  │
                                        gold.previsoes ◀─────────── 399 probabilidades   └──────────────┘
                                        treino/teste marcado
```

---

## 2. Tabelas por camada

### bronze (o dado como chegou)

| Tabela | Origem | Linhas | Observação |
|---|---|---|---|
| `transacoes` | CSV | 6.383 | tudo `TEXT`, inclusive datas e valores |
| `catalogo` | JSON | 12 | snapshot, substituído a cada execução |
| `municipios_ibge` | API REST | 237 | snapshot |
| `clientes` | PostgreSQL | 388 | carga incremental pela data de cadastro |

Todas carregam `_ingerido_em` (com `DEFAULT now()`) e `_fonte`. Nenhuma tem
chave primária ou `CHECK`, de propósito: a camada precisa aceitar o dado sujo.

### silver (limpo e enriquecido)

| Tabela | Linhas | Chave | Restrições |
|---|---|---|---|
| `transacoes` | 5.809 | `transacao_id` | `quantidade BETWEEN 1 AND 20`, `valor > 0`, `preco_unitario > 0`, `idade BETWEEN 0 AND 120` |
| `clientes` | 388 | `cliente_id` | `idade BETWEEN 18 AND 120` |

Índices em `cliente_id`, `data` e no par `(cliente_id, data)`, que é o acesso
que a engenharia de features faz.

### gold (pronto para consumo)

| Objeto | Linhas | Observação |
|---|---|---|
| `features_clientes` | 399 | 44 features mais o alvo; estrutura gerada pelo Python |
| `features_dicionario` | 44 | tipo, **origem**, média, desvio, correlação |
| `previsoes` | 399 por rodada | probabilidade, faixa, conjunto (treino ou teste) |

`features_clientes` é a única tabela sem DDL declarado. O conjunto de colunas
depende das categorias presentes no dado: se surgir uma mesorregião nova, o
One-Hot cria mais uma coluna e um DDL fixo passaria a divergir. A tabela é
derivada e reproduzível, então é recriada a cada execução, com a chave primária
reposta em seguida.

### meta (proveniência e histórico)

| Tabela | Papel |
|---|---|
| `etl_execucoes` | cada passo de cada etapa, append-only |
| `controle_extracao` | marco da última extração por fonte |
| `treinos` | métricas dos 5 modelos de cada rodada, agrupadas por UUID |

O `--recriar` derruba bronze, silver e gold, e **preserva a meta**. Apagá-la
eliminaria justamente o registro de que o pipeline já rodou antes.

---

## 3. As junções da camada silver

Três junções, cada uma com contador explícito no log.

| # | De | Para | Chave | Resultado |
|---|---|---|---|---|
| 1 | transações | `bronze.clientes` | `cliente_id` | 5.674 casaram, 135 de 12 clientes sem cadastro |
| 2 | transações | `bronze.catalogo` | produto sem acento | 5.809 casaram, 0 sem custo |
| 3 | transações | `bronze.municipios_ibge` | cidade sem acento + UF | 5.808 casaram, 1 sem região |

### Quem vence quando as fontes discordam

O CRM é autoritativo para nome, e-mail, idade, cidade e estado. O extrato CSV
repete esses campos, mas sujos, porque é um dump operacional. Para os 12
compradores fora do cadastro, os valores do CSV limpo permanecem, e a coluna
`cliente_cadastrado` marca o caso.

A transação desses 12 **não é descartada**: a venda aconteceu, e removê-la
distorceria a receita para proteger um campo que não participa da análise.

### A chave de junção textual

Sempre o texto normalizado sem acento e em caixa baixa. O IBGE devolve
"Macapá"; o extrato traz "MACAPA", "  macapa" ou "Macapa  ". Sem normalizar, o
join casa quase nada e não reclama.

---

## 4. A resiliência da fonte externa

A API do IBGE é a única fonte fora do nosso controle. A coleta tem três
comportamentos, todos cobertos por teste:

| Situação | Comportamento | `origem` registrada |
|---|---|---|
| API responde | coleta normal, grava o cache | `api` |
| Uma UF falha | as outras entram, o cache cobre a que faltou | `api+cache` |
| API inteira fora | usa o cache da execução anterior | `cache` |

Sem cache e sem API, o pipeline para com mensagem explícita, em vez de gravar
uma tabela vazia.

---

## 5. Carga incremental

`meta.controle_extracao` guarda o marco já processado por fonte. Hoje só o CRM
usa o mecanismo, com a data de cadastro como marco:

```sql
SELECT chave, valor, atualizado_em FROM meta.controle_extracao;
```

Na primeira execução o marco não existe e a carga é completa. Nas seguintes, só
entram os cadastros posteriores ao marco. O `--recriar` ignora o marco e força
carga completa.

O `UPSERT` (`ON CONFLICT DO UPDATE`) depende da chave primária em `chave`, que é
por isso que ela existe.

---

## 6. O caminho da previsão na API

A rota `POST /prever` percorre exatamente as mesmas etapas do ETL, usando os
mesmos módulos:

```
histórico recebido
      │
      ▼  limpeza.limpar()                 as 7 etapas de limpeza
      │
      ▼  enriquecimento.aplicar(eng)      consulta bronze.catalogo e
      │                                   bronze.municipios_ibge
      ▼  features.construir()             agrega para uma linha
      │
      ▼  features.codificar(FEATURES)     One-Hot alinhado à lista salva
      │
      ▼  scaler.transform()               o scaler que veio no .pkl
      │
      ▼  modelo.predict_proba()
```

O passo de `codificar` com `colunas_esperadas` é o que impede o erro silencioso
mais comum em produção: cliente de uma região que não existia no treino teria
uma coluna a mais, e o modelo aceitaria o vetor com significado trocado. O
`reindex` descarta a categoria desconhecida, entra com zero nas ausentes e
preserva a ordem.
