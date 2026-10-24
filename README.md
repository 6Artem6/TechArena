## Что решает проект

TechArena — это система для построения точного контекста затронутого функционала по описанию действия пользователя в системе.

Вместо того чтобы просто искать по тексту в кодовой базе, проект пытается решить более сложную задачу: по сценарию или пользовательскому действию понять, какие сервисы, методы и участки кода реально вовлечены в выполнение этой операции. Результат — компактный, высокосигнальный контекст, который можно передать LLM для дальнейшего анализа, генерации решения, планирования правки или объяснения поведения системы.

Иными словами, проект не про “поиск строк в репозитории”, а про “сопоставление пользовательского действия с кодом, который это действие затрагивает”.

---

## Почему это важно

В реальных системах чаще всего нет возможности просто запустить приложение и наблюдать за стеком вызовов в каждом сценарии. При этом разработчику или агенту ИИ нужно быстро понять:
- какие модули участвуют в выполнении действия,
- какие сервисы и методы являются входными точками,
- какие зависимости и ветви исполнения важны,
- где именно в проекте находится логика, затронутая этим действием.

Обычный grep или naive semantic search быстро теряют качество: они дают слишком много шумных результатов или не учитывают причинно-следственные связи между сервисами и бизнес-операциями. TechArena пытается закрыть эту нишу: построить не “текстовый поиск по коду”, а “контекст затронутого функционала по действию пользователя”.

---

## Какой результат даёт система

На входе проект принимает:
- описание действия, сценария или бизнес-операции;
- проектный граф или предварительно подготовленные данные о структуре кода;
- доменный контекст и метаданные, построенные офлайн.

На выходе система формирует:
- вероятные entry points / обработчики;
- затронутые сервисы и методы;
- граф/замыкание связанных участков;
- узкий, релевантный контекст для последующего использования LLM.

Такой подход полезен там, где нужно:
- понять влияние действия на код без запуска системы,
- быстро локализовать функциональные области,
- уменьшить шум при анализе больших микросервисных проектов,
- обеспечить LLM контекстом, достаточным для точного и экономного reasoning.

---

## Чем проект отличается от обычного semantic code graph

Проект не является “универсальным static-analysis движком для любого репозитория” в классическом смысле. Его ценность — в другом:

- он ориентирован не на абстрактный кодовый граф как таковой,
- а на привязку пользовательского действия к актуальному бизнес-контексту системы;
- он работает по уже построенному графу и офлайн-обогащённым данным;
- он делает упор на релевантность контекста, а не на полноту сырого графа.

Сильная сторона решения — не в “полной универсальности”, а в том, что оно строит полезный, отсечённый и интерпретируемый подмножество проекта под конкретный сценарий. Это особенно важно для сложных микросервисных архитектур, где статические связи не всегда явно отражают бизнес-логику и пользовательский поток.

---

## Почему это интересно с точки зрения инженерной ценности

Проект объединяет несколько практических подходов:

- графовое представление о структуре системы;
- семантический поиск по тексту и коду;
- обогащение контекста через domain metadata и service passports;
- фильтрация и pruning несвязанных частей;
- повторное использование офлайн вычислений для ускорения онлайн запросов.

Это делает систему близкой к инженерному подходу “offline understanding + online relevance selection”: сначала строится контекст и знания, затем на каждом сценарии выбирается только то, что действительно важно.

---

## Ограничения и корректная оценка

Важно понимать, что проект не пытается заменить полноценный runtime tracing или универсальный static analyzer для всех языков и всех типов приложений.

Его главные ограничения:
- он оптимизирован под проектный граф и проектный контекст;
- часть логики остаётся доменно-специфичной;
- качество сильно зависит от подготовленных офлайн кэшей и правил;
- он лучше работает в рамках одного проекта или группы похожих систем, чем как “универсальный движок для любого репозитория”.

Это не минус, а часть архитектурной логики: система должна быть сильной там, где важен контекст и релевантность, а не абстрактная полнота.

---

## В каком формате проект существует сейчас

Текущий проект реализован как исследовательский инженерный пайплайн, ориентированный на:
- использование графа проекта,
- семантическое сопоставление по описанию сценария,
- онлайн-выбор затронутых участков кода,
- офлайн-генерацию доменных и проектных кэшей.

Подход хорошо подходит для сценариев, где нет запуска приложения, но нужно получить точный и узкий контекст для дальнейшего анализа или LLM-моделирования.

---

# TechArena — Execution Trace Predictor

Предиктор execution trace для микросервисного проекта [Train Ticket](tt-challenge/README.md).  
По текстовому описанию сценария (S0–S7) предсказывает граф вызовов: какие методы выполнились, какие вызовы между сервисами произошли, к каким таблицам обратился код.

**Текущий best на платформе: 0.818330** (avg IoU по S1–S7)

---

## Итог

TechArena — это не просто “поиск по коду”, а инженерная система для построения контекста затронутого функционала по пользовательскому действию. Её ценность состоит в том, что она помогает перевести естественное описание операции в набор релевантных сервисов, методов и участков кода, тем самым делая LLM-аналитику более точной, экономной и осмысленной.

Это особенно актуально для комплексных микросервисных систем, где код сам по себе не всегда объясняет бизнес-процесс, а главное — не то, что написано, а то, что реально затронуто действием пользователя.

---

## Быстрый старт

### Первый запуск (сгенерировать все кеши)

```bash
# Установить зависимости
pip install -r requirements-dev.txt

# Сгенерировать все offline кеши (Ollama должен быть запущен)
python scripts/gen_all_caches.py

# Запустить predict + упаковать + отправить
python scripts/pack.py
python scripts/submit.py
```

> **Примечание:** `predict.py` автоматически запускает `gen_all_caches.py` при отсутствии критических кешей (`fsm_transitions.json`, `sibling_metadata.json`, `project_config.json`, `optional_patterns.json`).

### Повторный запуск (кеши есть)

```bash
python scripts/pack.py          # regenerate submission + zip
python scripts/submit.py        # upload + poll result
```

---

## Архитектура

### Offline: генерация кешей

```
skeleton.json.gz + targets/*.java
        │
  ┌─────▼─────────────────────────────────────────────────────────────┐
  │  gen_all_caches.py  (единый скрипт, запускает шаги по порядку)   │
  └─────┬─────────────────────────────────────────────────────────────┘
        │
  [1]   gen_fsm_transitions.py  →  cache/fsm_transitions.json
  [2]   gen_fsm_conditions.py   →  аннотирует fsm_transitions.json (AST + Qwen)
  [3]   gen_sibling_metadata.py →  cache/sibling_metadata.json
  [4]   gen_service_profiles.py →  cache/service_profiles_v2.pkl
                                    cache/service_passports.pkl
                                    cache/project_profile.json
  [5]   gen_project_passport.py →  cache/project_passport.json
  [6]   gen_category_clusters.py → cache/auto_categories.json
  [7]   gen_project_config.py   →  cache/project_config.json
                                    cache/optional_patterns.json
```

### Online: predict pipeline

```
scenarios/S*.json (action text per step)
        │
  step_typer.py          classify → StepInfo
  ├─ RuleEngine          yaml classification_rules → train_type, seat_class,
  │                        pay_type, is_refused
  ├─ Qwen classify_step  StepParams (9 полей) + domain context из project_passport
  └─ token_injection     extra BM25 tokens из token_injection_rules (yaml)
        │
  oracle.py / Oracle     FSM: INIT → PAID → CANCELLED
  └─ excluded_modules    food/insurance/consign если не заказано
        │
  searcher.py / enhanced_ground
  ├─ BM25                doc_text: verb+path+name+module+synonyms
  ├─ BGE-M3              semantic reranker (sentence-transformers)
  ├─ CrossEncoder        top_k=30, кеш в ce_scores_cache.pkl
  ├─ τ-window            threshold = 0.05
  └─ sibling force-swap  ts-order ↔ ts-order-other (из sibling_metadata.json)
        │
  closure.py / pruned_closure
  ├─ BFS                 calls + remote_call + data_access edges
  ├─ CFG pruning         G-train → блокирует *-other-service
  ├─ DI contracts        Spring @Autowired
  └─ ORM                 entity loading (data edges)
        │
  submission.json        {executes, calls, remote, data}
```

---

## Структура проекта

```
src/
├── predict.py              # точка входа online pipeline
├── domain_config.yaml      # classification_rules, token_injection_rules,
│                           # safe_keyword_step_types, tier4_executes_tokens
├── _config.py              # загрузка yaml + merge project_config.json
├── rule_engine.py          # RuleEngine: yaml rules → setattr(StepInfo, ...)
├── localization/
│   ├── step_typer.py       # StepInfo: RuleEngine + Qwen + token_injection
│   ├── searcher.py         # enhanced_ground(): BM25 + BGE-M3 + CE + swap
│   ├── qwen_encoder.py     # Qwen: classify_step() + decompose() + domain ctx
│   ├── passport_generator.py # Qwen: business_summary + synonyms per method
│   ├── hub_scorer.py       # топологический boost hub-сервисов
│   └── fsm_transitions.py  # load/forward_expand/reverse_expand
├── trace/
│   ├── closure.py          # pruned_closure(): BFS + CFG + DI + ORM
│   └── oracle.py           # Oracle FSM + excluded_modules
└── core/
    ├── skeleton_index.py   # Index: граф + handlers + modules
    └── bm25.py             # BM25 реализация

scripts/
├── gen_all_caches.py       # *** единый скрипт генерации кешей ***
├── gen_fsm_transitions.py  # шаг 1: FSM transitions из скелета
├── gen_fsm_conditions.py   # шаг 2: AST-аннотация conditions
├── gen_sibling_metadata.py # шаг 3: G/D sibling пары сервисов
├── gen_service_profiles.py # шаг 4: ServiceProfile + паспорта методов
├── gen_project_passport.py # шаг 5: паспорт проекта (Qwen 3B)
├── gen_category_clusters.py # шаг 6: токен-кластеры (BGE-M3 + Qwen + CE)
├── gen_project_config.py   # шаг 7: project_config.json + optional_patterns
├── gen_step_token_bridges.py # P3: BM25 co-occurrence → step_token_bridges.pkl
├── gen_classification_rules.py # аудит: Qwen генерирует rules из enum values
├── pack.py                 # генерирует submission.json + zip
├── submit.py               # отправляет на olympic.nsu.ru + polling
├── eval.py                 # локальная IoU оценка по S0 gold
└── diff_predictions.py     # сравнение двух submission.json

cache/                      # AUTO-GENERATED, не коммитить
├── fsm_transitions.json    # FSM: entry_nid → downstream handlers
├── sibling_metadata.json   # G/D пары: {service → {is_g_side, sibling}}
├── project_config.json     # auto-derived: naming, optional_groups, arch_tokens
├── optional_patterns.json  # regex: has_food/no_food/has_insurance/...
├── service_passports.pkl   # {filename:hash → {method → {summary, synonyms}}}
├── service_profiles_v2.pkl # {service → ServiceProfile}
├── project_profile.json    # Qwen теги проекта
├── project_passport.json   # {domain, main_features, user_journeys}
├── auto_categories.json    # [{category_id, tokens, cohesion, verified}]
├── qwen_encoder_cache.pkl  # {("cls_v3", action) → StepParams}
└── ce_scores_cache.pkl     # {(query, candidate) → float}

tt-challenge/
├── skeleton/trainticket.json.gz  # граф-скелет (nodes + edges)
├── targets/trainticket/          # исходники Java (41 микросервис)
├── scenarios/S0-S7.json          # описания сценариев
├── gold/S0.json                  # правильный ответ для S0
└── context/                      # начальное состояние БД
```

---

## Команды

### Генерация кешей

```bash
# Сгенерировать все кеши (пропускает уже существующие)
python scripts/gen_all_caches.py

# Только отсутствующие (явно)
python scripts/gen_all_caches.py --missing

# Принудительно всё заново
python scripts/gen_all_caches.py --force

# Начать с определённого шага (например пересчитать начиная с паспорта)
python scripts/gen_all_caches.py --from-step 5

# Запустить только конкретные шаги
python scripts/gen_all_caches.py --only 5 6 7

# Показать статус всех кешей
python scripts/gen_all_caches.py --list

# Без Qwen (детерминированные шаги только, быстро)
python scripts/gen_all_caches.py --no-qwen
```

### Predict + оценка

```bash
# Один сценарий
python src/predict.py tt-challenge/scenarios/S0.json -o submission.json

# Все сценарии
python src/predict.py tt-challenge/scenarios/S{0..7}.json -o submission.json

# IoU по S0 (gold доступен)
python scripts/eval.py submission.json

# Запустить predict + оценить сразу
python scripts/eval.py --run

# Сравнить два submission
python scripts/diff_predictions.py submission_A.json submission_B.json
```

### Pack + submit

```bash
# Сгенерировать submission.json + упаковать zip
python scripts/pack.py

# Только упаковать (submission.json уже есть)
python scripts/pack.py --no-regen

# Отправить последний zip + дождаться результата
python scripts/submit.py

# Только посмотреть последние результаты
python scripts/submit.py --scores -n 10
```

---

## Ключевые архитектурные решения

### 1. G/D sibling pruning

Сервисы-пары (`ts-order-service` ↔ `ts-order-other-service`) обслуживают разные типы поездов:
- G-type (высокоскоростные): `ts-order-service`, `ts-travel-service`, `ts-preserve-service`
- D/K/T/Z-type (обычные): `ts-order-other-service`, `ts-travel2-service`, `ts-preserve-other-service`

`sibling_metadata.json` содержит маппинг. `pruned_closure()` блокирует в BFS не тот side.  
`enhanced_ground()` выполняет **sibling force-swap**: если BM25/BGE-M3 нашёл G-side handler, а сценарий D-train — автоматически заменяет на sibling.

### 2. Oracle excluded_modules

`Oracle` анализирует историю шагов и отключает целые модули:
- `ts-food-*` сервисы — если `has_food=False`
- `ts-assurance-service` — если `has_insurance=False`
- `ts-consign-*` сервисы — если `has_luggage=False` и в истории нет `add_consign`

Список модулей per group читается из `cache/project_config.json:optional_service_groups` — portable, не хардкод.

### 3. Service passports (NLP enrichment)

250 методов имеют паспорта с `business_summary` и `synonyms` (генерирует Qwen).  
Два применения:
- `doc_text` для BM25/BGE-M3: `base + synonyms[:4]`
- `doc_text` для CrossEncoder: `base + java_ctx + business_summary + all_synonyms`

Ключ кеша: `(filename, md5_hash)` → переиспользуется при изменении кода автоматически.

### 4. Project passport → Qwen context

`cache/project_passport.json` агрегирует паспорта сервисов → Qwen 3B → `{domain, main_features, user_journeys}`.  
Используется в трёх местах:
- `qwen_encoder.classify_step()`: domain context prepend к системному промпту
- `gen_category_clusters.py`: project_context в label_cluster_qwen()
- `gen_project_config.derive_infra_module_tokens()`: domain context + dynamic EXCLUDE

### 5. Data-driven RuleEngine (zero hardcode)

Все regex-правила классификации — в `domain_config.yaml:classification_rules`.  
`RuleEngine.evaluate_into(text, step_info)` через `setattr` — Python не знает имён полей.  
`token_injection_rules` (yaml) заменяют if-блоки в `_build_extra_tokens()`.  
`qwen_service_mappings` (yaml) описывают маппинг Qwen StepParams → StepInfo поля.

### 6. is_refused filter

Шаги с `"the system refuses/rejects/denies"` → `is_refused=True` → `entry_handlers=∅`.  
Критично для S7: без фильтра 29 лишних FP методов.

---

## Метрики (платформа, best submission)

| Сценарий | IoU    | Описание |
|----------|--------|----------|
| S1       | 0.844  | G-train, страховка, полное бронирование |
| S2       | 0.769  | D-train, отмена + реброкирование |
| S3       | 0.817  | Бронирование с luggage consignment |
| S4       | 0.868  | G-train, еда, полный flow |
| S5       | 0.773  | D-train, transit search, advanced |
| S6       | 0.845  | G-train, страховка + еда |
| S7       | 0.813  | Refused cancel/rebook шаги |
| **avg**  | **0.818330** | |

Метрика — **Jaccard IoU** по объединению 4 типов рёбер: `executes`, `calls`, `remote`, `data`.

---

## Зависимости

```bash
pip install -r requirements-dev.txt
```

Основные: `rank-bm25`, `sentence-transformers` (BGE-M3), `transformers` (CrossEncoder).  
LLM: **Ollama** с моделью `qwen2.5-coder:3b` (для offline генерации кешей и online classify).

```bash
# Установить и запустить Ollama
ollama pull qwen2.5-coder:3b
ollama serve
```

Конфиг `.env` в корне проекта (для submit):
```
CGISESSID=xxxxxxx
__ddg1_=xxxxxxx
```
