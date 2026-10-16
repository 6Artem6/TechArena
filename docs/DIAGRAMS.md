# Архитектурные диаграммы — Execution Trace Predictor

> C4-модель + детальные схемы ключевых компонентов.
> Технология · Подход · Условие · Результат · Переход · Связи

---

## C4 Level 1 — System Context

Что является входом, что системой, кто оценивает.

```mermaid
flowchart TB
    classDef ext fill:#ddd,stroke:#999,color:#000
    classDef sys fill:#1168bd,stroke:#0b4884,color:#fff
    classDef user fill:#08427b,stroke:#052e56,color:#fff

    U["👤 Команда хакатона\n─────────────────\nРазрабатывает предиктор\nна Python"]:::user

    subgraph EXT["Организаторы (входные данные)"]
        SK[("🗄 Skeleton Graph\ntrainticket.json.gz\n15 229 nodes · 19 573 edges")]:::ext
        TT[("📁 targets/trainticket/\nJava + JS исходники\n41 сервис")]:::ext
        SC[("📄 scenarios/S0–S7.json\nТекстовые шаги\nen + zh")]:::ext
        CTX[("📋 context/\naccount · balance\nsystem_date")]:::ext
        GOLD[("✅ gold/S0.json\nЭталон S0\n(S1-S7 скрыты)")]:::ext
    end

    subgraph PRED["Execution Trace Predictor (Python · Local-First)"]
        P["🧠 Predictor\n──────────────────────────\n• Загружает скелет\n• Находит entry points (BM25)\n• Обходит граф (BFS)\n• Отсекает ветви (Oracle+Pruner)\n• Формирует submission.json"]:::sys
    end

    subgraph OUT["Выход"]
        SUB[("📤 submission.json\n{project, scenarios}\nexecutes/calls/remote/data")]:::ext
        EVAL["📊 evaluate_golden.py\nIoU по 4 типам\nS1–S7 hidden"]:::ext
    end

    SK & TT & SC & CTX --> P
    GOLD -.->|"только S0\nдля отладки"| P
    P --> SUB
    SUB --> EVAL
    U -->|"разрабатывает и запускает"| P
```

---

## C4 Level 2 — Container Architecture

Основные технологические блоки системы.

```mermaid
flowchart LR
    classDef container fill:#438dd5,stroke:#2e6da4,color:#fff
    classDef store fill:#f5a623,stroke:#c4841b,color:#000
    classDef ext fill:#bbb,stroke:#888,color:#000

    SK_IN[("trainticket.json.gz")]:::ext
    TGT[("targets/trainticket/")]:::ext
    SC_IN[("scenarios/*.json")]:::ext
    CTX_IN[("context/")]:::ext

    subgraph Offline["Offline (единожды при старте)"]
        IDX["skeleton/Index\n──────────────────\nLoads skeleton\nHandlers · CHA · BFS\nwiring · stmts\n─\nPython + NetworkX"]:::container
        ENR["enrichment/walker.py\n──────────────────\nAST-обход исходников\nExtract: branch conditions,\nlogger tags, method bodies\n─\nPython + tree-sitter"]:::container
        LOC_IDX["localization/BM25Index\n──────────────────\ncode_doc + ui_doc\nза каждый handler\n─\nPython + BM25Okapi"]:::container
    end

    subgraph Online["Online (per scenario)"]
        LOC["localization/searcher.py\n──────────────────\nStep → entry NodeId\nToken Taxonomy\nStep type classification\n─\nBM25 + regex(oracle only)"]:::container
        ORC["trace/oracle.py\n──────────────────\nStateful context\nbalance · train_type\norder_status · dates\n─\nPython dataclass"]:::container
        BFS["trace/closure.py\n──────────────────\nBFS по скелету\ncalls/runtime_dispatch\nremote_call/data_access\n─\nNetworkX + Index"]:::container
        PRN["trace/pruner.py\n──────────────────\n3a: text-deterministic\n3b: oracle-context\nshould_follow(edge)\n─\nPython + enrichment"]:::container
        SUB["output/submission.py\n──────────────────\nDedup · stmt: expand\nFormat JSON\n─\nPython"]:::container
    end

    PHASE4["adapters/vector/ (Phase 4)\n──────────────────\nBGE-M3 embeddings\nLanceDB hybrid search\n─\n(запасной вариант)"]:::container

    SK_IN --> IDX
    TGT --> ENR
    IDX & ENR --> LOC_IDX

    SC_IN --> LOC
    CTX_IN --> ORC
    LOC_IDX --> LOC
    LOC --> ORC
    ORC --> BFS
    IDX --> BFS
    BFS <--> PRN
    ENR --> PRN
    BFS --> ORC
    BFS --> SUB
```

---

## C4 Level 3 — Component: trace/ (ядро предиктора)

Детали самого сложного контейнера — пошаговый обход с управлением состоянием.

```mermaid
flowchart TD
    classDef comp fill:#85bbf0,stroke:#5d8ab0,color:#000
    classDef data fill:#f5a623,stroke:#c4841b,color:#000
    classDef decision fill:#ffd700,stroke:#b8a000,color:#000

    ENTRY["entry NodeId\n(из localization/)"]:::data
    SK_GRAPH[("skeleton Index\n+ enrichment")]:::data

    subgraph oracle["oracle.py — Oracle (dataclass)"]
        OB["balance: int = 10_000"]:::comp
        OD["trip_date: date = 2026-10-01"]:::comp
        OSY["system_date: date = 2026-09-15"]:::comp
        OTT["train_type: str = ''"]:::comp
        OSC["seat_class: str = ''"]:::comp
        OOS["order_status: str = ''"]:::comp
        OHF["has_food / insurance / luggage: bool"]:::comp
        OAS["active_services: set(str)"]:::comp
    end

    subgraph closure["closure.py — BFS (per step)"]
        Q["Queue: entry, ..."]:::comp
        VISIT["visited: set(NodeId)"]:::comp
        EDGES["edges: set(tuple)"]:::comp
        BFS_LOOP["BFS loop:\npop → get_outgoing_edges()\n→ pruner.should_follow?\n→ enqueue"]:::comp
    end

    subgraph pruner["pruner.py — Pruner"]
        P3A["3a: text_params\ntrain_type · seat_class\nhas_food (из шага)"]:::comp
        P3B["3b: context\nbalance ≥ price\norder.status\nservice_active"]:::comp
        PDEC{"should_follow\n(edge)?"}:::decision
    end

    RESULT["(methods, calls_edges,\nremote_edges, data_edges)"]:::data

    ENTRY --> Q
    SK_GRAPH --> BFS_LOOP
    Q --> BFS_LOOP
    BFS_LOOP --> PDEC
    PDEC --"yes"--> VISIT
    PDEC --"no"--> BFS_LOOP
    VISIT --> EDGES
    EDGES --> BFS_LOOP
    P3A & P3B --> PDEC
    oracle --> P3B
    BFS_LOOP --"обновить oracle\n(order placed, paid)"--> oracle
    BFS_LOOP --> RESULT
```

---

## Пайплайн — End-to-End Flowchart

Полный путь от сценария до submission.json.

```mermaid
flowchart TD
    classDef input fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef process fill:#bbdefb,stroke:#1565c0,color:#000
    classDef decision fill:#fff9c4,stroke:#f57f17,color:#000
    classDef output fill:#ffe0b2,stroke:#e65100,color:#000
    classDef skip fill:#f5f5f5,stroke:#9e9e9e,color:#555

    S[("Сценарий\nsteps: text, ...")]:::input
    CTX_IN[("Context\nbalance=10000\ndate=2026-09-15")]:::input

    INIT["Oracle.init(context)"]:::process
    FOREACH["для каждого step i"]:::process

    TYP["Step Typer\nклассифицировать:\nBUY | CANCEL | QUERY | REBOOK"]:::process

    NLP["BM25 Search\ncode_doc + ui_doc\n→ top-K handlers"]:::process

    CONF{"confidence\n≥ threshold?"}:::decision
    HIGH_C["entry = top-1 handler\n(HIGH confidence)"]:::process
    LOW_C["entry = PARTIAL_ENTRY\nвсе handlers сервиса\n(LOW / ambiguous)"]:::process

    ORC_UP["Oracle.parse_step(text)\ntrain_type · seat_class\nhas_food (3a params)"]:::process

    BFS["BFS closure(entry, oracle)\nby deployment_unit\nvia skeleton edges"]:::process

    subgraph edge_handling["Обработка рёбер в BFS"]
        E_CALLS["calls / runtime_dispatch\n→ pruner.should_follow_3a?\n→ pruner.should_follow_3b?"]:::process
        E_REMOTE["remote_call\n→ oracle.is_service_active?\n→ предсказать remote edge"]:::process
        E_DATA["data_access\n→ добавить data edge"]:::process
        E_ABSTRACT["abstract method\n→ CHA через overrides\n→ конкретная impl"]:::process
    end

    ORC_POST["Oracle.update_after_step\norder_status · active_services"]:::process

    NEXT{"следующий\nшаг?"}:::decision
    AGG["Union всех шагов\nDedup\nExpand stmt: nodes\nJWTFilter per step"]:::process

    OUT[("submission.json\n{executes, calls,\nremote, data}")]:::output

    S --> INIT
    CTX_IN --> INIT
    INIT --> FOREACH
    FOREACH --> TYP
    TYP --> NLP
    NLP --> CONF
    CONF --"HIGH"--> HIGH_C
    CONF --"LOW"--> LOW_C
    HIGH_C & LOW_C --> ORC_UP
    ORC_UP --> BFS
    BFS --> E_CALLS
    BFS --> E_REMOTE
    BFS --> E_DATA
    BFS --> E_ABSTRACT
    E_ABSTRACT --> BFS
    BFS --> ORC_POST
    ORC_POST --> NEXT
    NEXT --"да"--> FOREACH
    NEXT --"нет"--> AGG
    AGG --> OUT
```

---

## Branch Selection — Схема ветвления

Как Oracle + Pruner принимают решения о следовании по рёбрам.

```mermaid
flowchart TD
    classDef check fill:#e3f2fd,stroke:#1565c0,color:#000
    classDef yes fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef no fill:#ffcdd2,stroke:#c62828,color:#000

    E["Ребро из BFS: source_cs → target_method"]

    T1{"3a: target по типу поезда?"}:::check
    T1Y["train_type=G → ts-travel\ntrain_type=other → ts-travel2"]:::yes
    T2{"3a: target по классу места?"}:::check
    T2Y["seat_class из текста → branch"]:::yes
    T3{"3a: target по еде/страховке?"}:::check
    T3Y["has_food из текста → ts-food"]:::yes

    B1{"3b: ветка по балансу?"}:::check
    B1Y["balance=10000 → всегда True\nследовать"]:::yes
    B1N["balance недостаточно → False\nпропустить"]:::no
    B2{"3b: ветка по order_status?"}:::check
    B2Y["order_status=PAID → cancel OK\nследовать"]:::yes
    B2N["order_status=INIT → cancel fail\nпропустить"]:::no
    B3{"3b: сервис уже вызван?"}:::check
    B3N["duplicate remote\nпропустить"]:::no

    REMOTE{"тип ребра = remote_call?"}:::check
    PRECIS["Precision: gold remote=0\nне предсказывать"]:::no
    DEF["следовать безусловно\ncalls / data_access"]:::yes

    E --> T1
    T1 -->|да| T1Y
    T1 -->|нет| T2
    T2 -->|да| T2Y
    T2 -->|нет| T3
    T3 -->|да| T3Y
    T3 -->|нет| B1
    B1 -->|True| B1Y
    B1 -->|False| B1N
    B1 -->|нет ветки| B2
    B2 -->|PAID| B2Y
    B2 -->|INIT| B2N
    B2 -->|нет ветки| B3
    B3 -->|duplicate| B3N
    B3 -->|нет| REMOTE
    REMOTE -->|remote_call| PRECIS
    REMOTE -->|calls/data| DEF
```

---

## Skeleton — Модель данных

Структура узлов и рёбер в trainticket.json.gz.

```mermaid
erDiagram
    MODULE {
        string name
        string path
        string artifact_id
        list deployment_units
    }
    FILE {
        string path
        string module
    }
    TYPE {
        string fqn
        string type_kind
        list annotations
        list modifiers
    }
    METHOD {
        string name
        string descriptor
        string return
        list annotations
        bool abstract
        string module
        int line
    }
    PARAMETER {
        string name
        string type_raw
        int index
    }
    CALL_SITE {
        string method
        string callee
        string receiver_type
        list string_args
        int line
    }
    DATA_RESOURCE {
        string id
    }
    CONFIG_ENTRY {
        string key
        string value
        string module
    }

    MODULE ||--o{ FILE : "contains"
    FILE ||--o{ TYPE : "contains"
    TYPE ||--o{ METHOD : "declares"
    TYPE ||--o{ FIELD : "declares"
    METHOD ||--o{ PARAMETER : "has_parameter"
    CALL_SITE }o--|| METHOD : "method (caller)"
    CALL_SITE ||--o{ METHOD : "calls"
    CALL_SITE ||--o{ METHOD : "remote_call"
    CALL_SITE ||--o{ DATA_RESOURCE : "data_access"
    CALL_SITE ||--o{ METHOD : "runtime_dispatch"
    METHOD ||--o{ METHOD : "overrides (impl→abstract)"
    MODULE ||--o{ MODULE : "depends_on"
    MODULE ||--o{ CONFIG_ENTRY : "has"
```

---

## Sequence — Обработка одного шага сценария

Детальный поток между компонентами для шага «купить билет».

```mermaid
sequenceDiagram
    autonumber
    participant SC as scenarios/S1.json
    participant ST as StepTyper
    participant LOC as BM25Searcher
    participant ORC as Oracle
    participant BFS as BFSClosure
    participant PRN as Pruner
    participant IDX as skeleton/Index
    participant OUT as submission.py

    SC->>ST: step = "Buy a ticket G train, first class, with food"
    ST->>ST: classify → BUY_TICKET
    ST->>ORC: parse_step(text)\n→ train_type='G', seat_class='FIRST', has_food=True
    ORC-->>ORC: update params

    ST->>LOC: query = "Buy ticket train first class food"
    LOC->>LOC: BM25.rank(query) over code_docs+ui_docs
    LOC-->>ST: top-1 handler: method:ts-preserve-service:...PreserveServiceImpl#preserve(...)

    ST->>BFS: closure(entries=[handler], oracle)
    BFS->>IDX: outgoing edges from handler

    loop BFS iteration
        IDX-->>BFS: edge(call_site→target, type=calls)
        BFS->>PRN: should_follow(edge, oracle)
        alt train_type=='G' и target=ts-travel-service
            PRN-->>BFS: True
        else target=ts-travel2-service
            PRN-->>BFS: False (skip)
        end

        IDX-->>BFS: edge(call_site→ts-order-service.handler, type=remote_call)
        BFS->>PRN: should_follow(remote_edge, oracle)
        PRN->>ORC: is_service_active('ts-order-service')?
        ORC-->>PRN: False (первый вызов)
        PRN-->>BFS: True
        BFS->>ORC: mark_active('ts-order-service')

        IDX-->>BFS: edge(call_site→res:table:order.orders, type=data_access)
        BFS-->>BFS: add data edge (безусловно)
    end

    BFS->>ORC: update_after_step()\n→ order_status='INIT'
    BFS-->>OUT: (methods, calls_edges, remote_edges, data_edges)

    OUT->>OUT: expand stmt: nodes\nadd JWTFilter\ndedup
    OUT-->>SC: step result appended
```

---

## Стек технологий — Сводная карта

```mermaid
flowchart LR
    subgraph PARSING["Parsing"]
        TS["tree-sitter"]
        TS_J["Java"]
        TS_JS["JavaScript"]
        TS_USE1["branch conditions\nиз targets/"]
        TS_USE2["UI context\nts-ui-dashboard"]
        TS --> TS_J & TS_JS
        TS --> TS_USE1 & TS_USE2
    end

    subgraph GRAPH["Graph"]
        NX["NetworkX"]
        NX_B["BFS closure"]
        NX_C["CHA via overrides"]
        SKI["Skeleton Index\n15 229 nodes\n19 573 edges"]
        NX --> NX_B & NX_C
        NX --> SKI
    end

    subgraph NLP["NLP Search"]
        BM["BM25Okapi\ncode_doc + ui_doc"]
        TOK["Token Taxonomy\nСистемные · Доменные\nСлужебные · Инфра"]
        BGE["Phase 4: BGE-M3\ndense+sparse\nLanceDB"]
    end

    subgraph STATE["State"]
        ORC["Oracle dataclass\nbalance · order_status\ntrain_type · seat_class\nactive_services"]
    end

    subgraph OUTPUT["Output"]
        SUB["submission.json\nexecutes · calls\nremote · data"]
        IOU["IoU evaluation\nJaccard per type\nS1-S7 hidden"]
        SUB --> IOU
    end
```
