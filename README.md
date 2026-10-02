# Pipeline de dados e previsão de churn em e-commerce

**Projeto Integrador (T15)**
Fundamentos de Banco de Dados (BDED-2026.1)
Especialização em Projetos em Inteligência Artificial, UNIFAP Digital
Aluno: Sandro · Prof. Dr. Adolfo Colares

Pipeline completo de dado bruto a modelo de IA, com quatro fontes heterogêneas,
armazenamento em camadas no PostgreSQL, engenharia de features, comparação de
modelos e uma API que serve o resultado.

```bash
python pipeline.py        # do zero ao modelo treinado, em ~14 segundos
```

---

## 1. O problema

Um e-commerce de informática e acessórios quer saber **quais clientes estão
prestes a parar de comprar**, para agir antes que isso aconteça.

A pergunta não é acadêmica. Reter um cliente custa um cupom de desconto; perder
um custa todo o faturamento futuro dele. O problema é que a equipe de retenção
não consegue abordar 400 pessoas, então precisa de uma **ordem de prioridade**.

É isso que o projeto entrega: a carteira ordenada pela probabilidade de churn.
O modelo não adivinha quem vai sair (nenhum modelo faz isso), mas concentra os
casos reais no topo da lista. Nos 10% de maior risco, 100% dos clientes de fato
não voltaram a comprar; nos 10% de menor risco, apenas 9%.

**Definição de churn adotada:** o cliente não fez nenhuma compra na janela de
avaliação, que vai de 1º de outubro a 31 de dezembro de 2024.

---

## 2. Arquitetura

```
   FONTES                        CAMADAS NO POSTGRESQL              CONSUMO

┌──────────────────┐
│ 1  CSV           │──┐
│ transações       │  │       ┌─────────────────────────┐
└──────────────────┘  │       │ bronze                  │
┌──────────────────┐  │       │ o dado como chegou      │
│ 2  JSON          │──┤       │ tudo TEXT, sem descarte │
│ catálogo/custo   │  │       └───────────┬─────────────┘
└──────────────────┘  ├──────▶            │  limpeza + 3 junções
┌──────────────────┐  │       ┌───────────▼─────────────┐
│ 3  API REST      │──┤       │ silver                  │
│ IBGE municípios  │  │       │ limpo, tipado, com CHECK│
└──────────────────┘  │       │ transacoes + clientes   │
┌──────────────────┐  │       └───────────┬─────────────┘
│ 4  PostgreSQL    │──┘                   │  separação temporal
│ cadastro do CRM  │          ┌───────────▼─────────────┐      ┌──────────────┐
└──────────────────┘          │ gold                    │─────▶│ 7 views      │
                              │ 1 linha por cliente     │      │ API FastAPI  │
                              │ 44 features + alvo      │      └──────────────┘
                              └───────────┬─────────────┘
                                          │  treino de 5 modelos
                              ┌───────────▼─────────────┐
                              │ meta                    │      modelo_churn.pkl
                              │ execuções, marcos de    │
                              │ carga, registry         │
                              └─────────────────────────┘
```

O diagrama detalhado, com o fluxo por tabela, está em
[`docs/arquitetura.md`](docs/arquitetura.md).

### Por que camadas

Cada camada é um schema, com um papel próprio:

| Camada | Papel | Restrições |
|---|---|---|
| `bronze` | o dado como chegou da fonte | nenhuma, tudo `TEXT` |
| `silver` | limpo, tipado, enriquecido | PK, `CHECK`, índices |
| `gold` | pronto para consumo, um cliente por linha | PK |
| `meta` | proveniência e histórico | append-only |

A **bronze aceita lixo de propósito**. É o que permite ingerir `"31/02/2024"` e
`"450,00"` sem o banco recusar, e reprocessar a limpeza inteira sem voltar às
fontes (que podem estar fora do ar ou já ter mudado). Também preserva a
evidência: depois da limpeza, ninguém conseguiria provar que havia 185
duplicatas no arquivo original.

A **silver impõe as regras da limpeza como `CHECK`**. Se algum registro
inválido escapar da validação em Python, o banco recusa. São duas guardas
independentes para a mesma regra.

---

## 3. As quatro fontes

| # | Tipo | Conteúdo | Registros | Por que existe |
|---|---|---|---|---|
| 1 | CSV | extrato de transações | 6.383 | o fato central |
| 2 | JSON | catálogo de produtos | 12 | traz o **custo**, sem o qual não há margem |
| 3 | **API REST** | municípios do IBGE | 237 | mesorregião, que o extrato não tem |
| 4 | **PostgreSQL** | cadastro do CRM | 388 | fonte autoritativa dos dados do cliente |

A fonte 3 é a única externa de verdade. A API do IBGE está fora do nosso
controle e vai falhar em algum momento, então a coleta tem **cache em disco** e
**degradação por UF**: se um estado não responder, os outros entram assim mesmo
e o cache cobre o buraco. Com a API inteira fora do ar, o pipeline segue com o
dado da execução anterior, registrando no log que foi por ele.

A fonte 4 vive em um banco separado (`crm_origem`), simulando um sistema de
terceiros que o pipeline apenas lê. O cadastro **não tem 12 dos 400
compradores**, e essa lacuna é deliberada: sem ela o join casaria 100% e não
haveria decisão a tomar.

---

## 4. O que a integração produziu

Esta é a pergunta que justifica a arquitetura multi-fonte, e a resposta tem
duas partes.

### Ganho de qualidade: grande

Comparando com a limpeza de fonte única feita no laboratório P12:

| Indicador | Fonte única | Integrado |
|---|---|---|
| Idades imputadas pela mediana | 296 | **4** |
| Cidades marcadas como "Desconhecida" | 170 | **1** |
| E-mails inválidos | 43 | **1** |
| Transações com mesorregião | zero | 5.808 de 5.809 |
| Margem calculada | não | sim, 22% a 55% por categoria |

O que a limpeza de fonte única tinha que adivinhar, o cadastro do CRM forneceu.

### Ganho preditivo: pequeno

```sql
SELECT * FROM gold.vw_contribuicao_das_fontes;
```

| Origem | Features | Maior correlação | Relevantes (> 0,30) |
|---|---|---|---|
| extrato CSV | 33 | 0,506 | 14 |
| catálogo JSON | 3 | 0,379 | 1 |
| API IBGE | 6 | 0,156 | 0 |
| CRM PostgreSQL | 1 | 0,023 | 0 |
| junção CSV × CRM | 1 | 0,007 | 0 |

A única feature forte vinda da integração é `margem_total`, e ela correlaciona
**0,969 com `total_gasto`**: é o mesmo dado reescalado, não informação nova. A
que traz informação de fato é `margem_pct` (correlaciona apenas −0,53 com o
gasto, porque capta o mix de produtos), mas ela quase não move o alvo.

**A conclusão honesta:** a integração melhorou muito a qualidade do dado e quase
nada o poder preditivo. São coisas diferentes, e misturá-las seria vender o
projeto por algo que ele não fez.

---

## 5. Engenharia de features

De 5.809 transações para **399 clientes × 44 features**, com churn em 56,4%.

### A separação temporal

A definição intuitiva de churn ("está há mais de 90 dias sem comprar") tem um
defeito fatal se a variável "dias sem comprar" também virar feature: a resposta
fica dentro da pergunta. O modelo acerta quase tudo sem ter aprendido nada.

Por isso o tempo é cortado em duas janelas:

```
observação   2023-01-01 .. 2024-09-30    5.416 transações   constrói as features
avaliação    2024-10-01 .. 2024-12-31      393 transações   define o alvo
```

A pergunta usa dados de antes; a resposta vem de depois.

### Os seis grupos

| Grupo | Quantas | Exemplos |
|---|---|---|
| Agregações do histórico | 13 | `total_gasto`, `ticket_medio`, `margem_pct` |
| Atributos temporais | 7 | `recencia_dias`, `meses_ativos`, `dias_desde_cadastro` |
| Janelas móveis e tendência | 8 | `compras_90d`, `tendencia_90d` |
| Razão de ciclos | 1 | `razao_recencia_intervalo` |
| Comportamento | 3 | `share_categoria_top`, `pct_fim_de_semana` |
| One-Hot | 12 | `meso_*`, `canal_*`, `pgto_*` |

### A feature que mais pesa

`razao_recencia_intervalo = recencia_dias / intervalo_medio`

A recência isolada não distingue dois casos opostos. Quem compra a cada 15 dias
e está há 90 sem comprar está seis ciclos fora do padrão **dele**; quem compra a
cada 120 dias e está há 90 está dentro do normal. A recência é a mesma nos dois,
90 dias. A razão distingue, e ela terminou como o maior coeficiente do modelo.

### Geografia por mesorregião, não por cidade

A mesma informação em granularidade mais grossa: seis colunas binárias em vez de
dez. Com 299 clientes de treino, cada coluna esparsa a menos é uma chance a
menos de o modelo ajustar ruído. Usar as duas seria redundância, já que
mesorregião agrupa municípios.

---

## 6. Modelos e resultados

Cinco algoritmos comparados, avaliados no conjunto de teste e por validação
cruzada de cinco divisões:

| Modelo | ROC AUC (teste) | Validação cruzada | F1 | Acurácia |
|---|---|---|---|---|
| **Regressão Logística** (escolhido) | **0,731** | 0,818 ± 0,042 | 0,684 | 0,66 |
| Random Forest | 0,714 | 0,829 ± 0,048 | 0,732 | 0,70 |
| KNN (k=15) | 0,697 | 0,826 ± 0,047 | 0,739 | 0,71 |
| Gradient Boosting | 0,694 | 0,811 ± 0,036 | 0,713 | 0,69 |
| Árvore de Decisão | 0,647 | 0,700 ± 0,041 | 0,636 | 0,62 |
| *Linha de base (sempre churn)* | *0,500* | *n/a* | *0,718* | *0,56* |

### Por que ROC AUC e não F1

Olhe a última linha. A linha de base, que não aprendeu nada e responde "churn"
para todo mundo, obtém **F1 de 0,718**, valor que supera a maioria dos modelos
treinados.

Não há mistério: quem responde sempre "churn" acerta todos os que de fato deram
churn (recall 1,0), e como 56% dos clientes realmente deram churn, a precisão já
nasce em 0,56. A média harmônica disso é alta. O F1 só enxerga a classe
positiva, e nunca olha para os verdadeiros negativos.

Selecionar por F1 escolheria o KNN. Em ROC AUC a linha de base marca exatamente
0,500, porque dá a mesma nota a todos e não ordena ninguém.

### O que o modelo vale na prática

```sql
SELECT * FROM gold.vw_ganho_por_decil;
```

| Decil de risco | % churn (geral) | % churn (só teste) |
|---|---|---|
| 1 (maior risco) | 100,0 | 100,0 |
| 2 | 87,5 | 77,8 |
| 5 | 65,0 | 60,0 |
| 9 | 22,5 | 35,7 |
| 10 (menor risco) | 5,1 | 9,1 |

A coluna **"só teste" é a que vale**. A geral inclui os 299 clientes que o
modelo viu no treino e sai otimista. Mesmo na coluna honesta o gradiente existe,
embora mais ruidoso, porque são apenas 5 a 15 clientes por decil.

---

## 7. Como executar

### Requisitos

- Python 3.12 e `pip install -r requirements.txt`
- PostgreSQL 16 acessível (o projeto cria sozinho os bancos `churn_dw` e
  `crm_origem`)

Sem um PostgreSQL à mão, o `docker-compose.yml` sobe um:

```bash
docker compose up -d --wait
```

Nesse caso, use `PG_HOST=localhost` no `.env`. O serviço tem healthcheck, então
o `--wait` só devolve o controle quando o banco estiver aceitando conexões, o
que evita o erro confuso de rodar o pipeline cedo demais.

### Configuração

Copie `.env.example` para `.env` e preencha a senha do banco. O `.env` está no
`.gitignore` e nenhuma credencial aparece no código ou no log (a URL de conexão
é registrada com a senha mascarada).

```bash
cp .env.example .env
```

### O pipeline

```bash
python pipeline.py                    # tudo, do zero ao modelo
python pipeline.py --etapa silver     # só uma etapa
python pipeline.py --recriar          # derruba bronze/silver/gold e refaz
```

O `--recriar` preserva o schema `meta`, que é histórico de execuções e de
treinos.

### A API

```bash
uvicorn api.main:app --reload
```

Documentação interativa em <http://127.0.0.1:8000/docs>.

| Rota | O que faz |
|---|---|
| `GET /saude` | modelo carregado e banco alcançável |
| `GET /modelo` | metadados, métricas e a linha de base |
| `GET /clientes/risco` | a carteira ordenada, com filtro por faixa e conjunto |
| `GET /clientes/{id}` | previsão e features de um cliente |
| `GET /regioes` | receita sob risco por mesorregião |
| `GET /metricas/modelos` | desempenho por algoritmo ao longo das rodadas |
| `GET /metricas/decis` | concentração de churn por decil |
| `GET /metricas/fontes` | o que cada fonte acrescentou |
| `GET /execucoes` | proveniência, os últimos passos do ETL |
| `POST /prever` | recebe o histórico de um cliente e devolve a probabilidade |

### Os testes

```bash
python -m pytest tests/ -v
```

### O caderno de análise

```bash
jupyter lab notebooks/analise_exploratoria.ipynb
```

Percorre as quatro camadas do banco com SQL, do dado cru ao resultado do modelo.
Exige que o pipeline já tenha rodado.

46 testes. Também rodam sem pytest: `python tests/test_silver.py`.

---

## 8. Estrutura

```
projeto_integrador/
├── pipeline.py               orquestrador, uma etapa por comando
├── requirements.txt
├── docker-compose.yml        PostgreSQL 16, para rodar sem instalar nada
├── .env.example              as chaves, sem os valores
│
├── sql/
│   ├── 01_camadas.sql        schemas, tabelas, CHECK, índices, comentários
│   └── 02_views.sql          7 views de consumo
│
├── src/
│   ├── config.py             configuração e leitura do .env
│   ├── db.py                 conexão, gravação, proveniência, carga incremental
│   ├── limpeza.py            regras de limpeza (compartilhadas com a API)
│   ├── enriquecimento.py     junções com catálogo e IBGE (idem)
│   ├── features.py           engenharia de features (idem)
│   ├── coleta/               um módulo por fonte
│   ├── etl/                  preparar, bronze, silver, gold
│   └── modelo/treinar.py     treino, seleção, registry, persistência
│
├── api/main.py               FastAPI
├── notebooks/
│   └── analise_exploratoria.ipynb   EDA sobre o banco, depois do pipeline
├── tests/                    46 testes
├── docs/arquitetura.md       o fluxo detalhado
├── dados/                    gerados pelo pipeline (fora do versionamento)
└── modelos/                  modelo_churn.pkl (idem)
```

### Uma fonte de verdade por regra

`limpeza.py`, `enriquecimento.py` e `features.py` são importados **tanto pelo
ETL quanto pela API**. Se cada lado tivesse a própria implementação, elas
divergiriam com o tempo, e o modelo passaria a receber em produção dados
diferentes dos que aprendeu, sem erro nenhum, só com previsões piores.

O teste `test_previsao_reproduz_o_que_esta_no_banco` pega os cinco clientes de
maior risco, manda o histórico deles pela API e exige que a probabilidade bata
com a gravada pelo ETL.

---

## 9. Decisões técnicas que merecem registro

### O critério de outlier é o preço unitário, não o IQR

O intervalo interquartil sobre o valor removeria 587 registros, liderados por
monitor (254), SSD (130) e headset (100), que são os produtos caros e legítimos
do catálogo. Ele não distingue "caro" de "errado".

O critério adotado compara o **preço unitário** com o teto do catálogo. O item
mais caro custa R$ 900, então qualquer preço unitário acima de R$ 1.500 não
corresponde a produto nenhum. Isolou exatamente os 72 erros de digitação sem
eliminar venda boa.

### Todo join tem contador

`merge(how="left")` nunca levanta erro. Quando a chave não casa, ele preenche
nulo e segue. Um join que deveria casar 100% e casa 3% produz uma tabela do
tamanho certo, com as colunas certas, cheia de nulos, e o problema só aparece
lá na frente como "o modelo não aprende".

As três junções da camada silver registram quantas linhas casaram, e a do
catálogo levanta exceção se sobrar produto sem custo.

### O scaler mora dentro do `.pkl`

Como arquivo separado ele é a peça que se esquece: o modelo aceita valores crus
sem reclamar e devolve probabilidade sem sentido. O artefato carrega modelo,
scaler, a lista de features na ordem correta e o limiar. O teste
`test_sem_o_scaler_a_previsao_muda` demonstra o estrago.

### `to_sql(if_exists="replace")` não é usado em lugar nenhum

Ele faz `DROP TABLE` e recria a tabela a partir dos dtypes do DataFrame.
Desaparece tudo o que o DDL declarou: chave primária, `CHECK`, índices,
`DEFAULT` e as colunas que o DataFrame não tem. A tabela continua ali, com o
nome certo e os dados certos, sem nenhuma das garantias.

Isso aconteceu de verdade durante o desenvolvimento (a bronze perdeu a coluna
`_ingerido_em`) e foi corrigido na raiz: `db.grava` só aceita `"substituir"`
(TRUNCATE mais INSERT) ou `"acrescentar"`.

### A ordem das chamadas ao gerador faz parte da semente

O gerador de dados produzia 6.388 linhas em vez de 6.383, com a mesma semente.
A causa: o sorteio do perfil do cliente tinha sido movido para dentro do literal
do dicionário, depois da idade, em vez de ficar antes dele. Duas chamadas ao
`random` trocadas de ordem, código visualmente equivalente, conjunto de dados
inteiramente diferente. O teste `test_ordem_do_sorteio_nao_mudou` fixa o
primeiro cliente esperado.

### Magnitude de coeficiente não é importância

As 31 colunas numéricas foram padronizadas (desvio 1,0) e as 13 binárias do
One-Hot não (desvio entre 0,2 e 0,5, porque cada categoria vale 1 em poucas
linhas). Para o mesmo efeito no resultado, a coluna esparsa precisa de um
coeficiente várias vezes maior, e sobe no ranking sem ter mais influência. O
ranking multiplica o coeficiente pelo desvio da própria coluna.

---

## 10. Limitações

**São 399 clientes, dos quais 100 no teste.** Diferenças de poucos pontos entre
os modelos cabem dentro da margem de incerteza dessa amostra. É o que explica a
distância entre a ROC AUC da validação cruzada (0,818, medida sobre 299 clientes
em cinco divisões) e a do teste (0,731, medida uma vez sobre 100).

**Os dados de transação são sintéticos**, gerados por script com semente fixa.
Cada cliente recebe um perfil de atividade oculto (fiel, declínio ou perdido)
que rege a probabilidade de comprar em cada mês, e é esse perfil que o modelo
tenta inferir. A coleta do IBGE, em compensação, é real.

**O corte temporal é fixo em 2024-10-01.** Três meses de janela de avaliação
equilibram dois riscos: uma janela curta demais classificaria como churn quem
apenas demorou a voltar, e uma longa demais empobreceria as features.

**O limiar de decisão é 0,5**, o padrão. Ele é um parâmetro de negócio, não do
modelo: como o falso negativo (perder o cliente) custa muito mais que o falso
positivo (um cupom desperdiçado), um corte abaixo de 0,5 é defensável. A decisão
cabe a quem conhece as margens.
