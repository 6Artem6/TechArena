# ADR-001: Стратегия парсинга исходного кода

**Статус:** Принято  
**Дата:** 2026-10-13

## Контекст

Проект анализирует полиглотный репозиторий (`train-ticket`): 47 сервисов, ~532 Java-файла + Python и Node.js. Нужен надёжный статический парсер без запуска кода.

## Решение

**Основной парсер: `tree-sitter`** (через `py-tree-sitter`)  
**Fallback для Java: `javalang`**

### Покрытие polyglot-сервисов в train-ticket

| Сервис | Язык | Фреймворк | Особенности |
|---|---|---|---|
| 47 Java-сервисов | Java | Spring Boot | Основная масса, RestTemplate/Feign |
| `ts-avatar-service` | Python | Flask | Face detection, без межсервисных вызовов |
| `ts-voucher-service` | Python | Tornado | `urllib.request` → `ts-order-service`, `ts-order-other-service` |
| `ts-news-service` | Go | `github.com/hoisie/web` | MongoDB закомментирован (shadow) |
| `ts-ui-dashboard` | JavaScript | Nginx (static) | HTML+JS, прямые API-вызовы через nginx-proxy |

### Распределение по языкам

| Язык | Парсер | Fallback |
|---|---|---|
| Java | tree-sitter-java | javalang |
| Python | tree-sitter-python | ast (stdlib) |
| JavaScript / TypeScript | tree-sitter-javascript | — |
| Go | tree-sitter-go | — |

### Стратегия fallback

1. Попытка парсинга через tree-sitter.
2. Если `tree-sitter` вернул частичное дерево или исключение → пробуем `javalang`.
3. Если оба не справились → файл помечается `PARTIAL_PARSED`, узел добавляется с минимальными метаданными (имя файла, сервис).
4. Парсинг одного файла не прерывает анализ всего репозитория (fail-safe изоляция).

### Изоляция ошибок: уровни обёртки (ADR-019)

Ошибка не поднимается выше своего уровня: файл → класс → метод. Каждый уровень защищён независимо.

```python
# Файл с нераспознаваемой кодировкой (китайские символы в комментариях)
text = file_path.read_text(encoding="utf-8", errors="replace")  # никогда не крашит

# Тестовые файлы исключаются по пути — O(1), не через парсинг содержимого
if "/test/" in str(src_file) or "/tests/" in str(src_file):
    continue

# При сбое одного класса — минимальный узел, остальные классы в файле продолжают
for class_node in find_class_nodes(tree):
    try:
        nodes.append(self._parse_class(class_node, text, file_path))
    except Exception:
        nodes.append(MinimalClassNode(
            file_path=file_path, parse_status="PARTIAL_PARSED"
        ))
```

`PARTIAL_PARSED` узел **гарантированно** содержит `file_path` и `service` — BFS не потеряет стартовую точку. Подробнее — `IsolatedParser` в ADR-019.

### Детекция закомментированного кода (shadow pipeline, ADR-016)

После основного парсинга для каждого файла выполняется **дополнительный проход**:

```python
# Шаг 1: tree-sitter находит все comment-узлы
comment_nodes = query_comments(tree, language)

# Шаг 2: Фильтр по размеру (≥2 строк или явная зависимость)
candidates = [c for c in comment_nodes if is_shadow_candidate(c)]

# Шаг 3: BGE-M3 классификатор — код или текст?
for candidate in candidates:
    if classifier.is_code(candidate.text, threshold=0.75):
        shadow_deps = extract_shadow_dependencies(candidate.text)
        emit_shadow_edges(shadow_deps, shadow=True)
```

Ошибки парсинга закомментированного кода игнорируются — результат всегда частичный (`PARTIAL_PARSED` допустим).

**Специальные случаи:**
- Javadoc `/** ... */` → не анализируется (документация, не код).
- Менее 2 строк или только однострочные литералы → пропускаем (слишком мало для зависимостей).
- Комментарии не на русском и не на английском (иероглифы и т.п.) → пропускаем классификацию.

## Обоснование

- `tree-sitter` быстрее и поддерживает все нужные языки в одном API.
- `javalang` точнее обрабатывает Java-специфику: сложные generics, многоуровневые аннотации (`@RequestMapping(value={...}, method=POST)`), inner classes.
- `ast` (stdlib Python) надёжен для простых Python-сервисов без зависимостей.

## Обоснование

- `tree-sitter` быстрее и поддерживает все нужные языки в одном API.
- `javalang` точнее обрабатывает Java-специфику: сложные generics, многоуровневые аннотации (`@RequestMapping(value={...}, method=POST)`), inner classes.
- `ast` (stdlib Python) надёжен для простых Python-сервисов без зависимостей.
- Python-сервисы `ts-voucher-service` и `ts-avatar-service` используют разные HTTP-библиотеки (urllib, Flask), tree-sitter-python покрывает оба.
- Go-сервис `ts-news-service` — tree-sitter-go; весь функциональный код закомментирован, shadow pipeline находит MongoDB зависимость.

## Последствия

- Зависимость от двух парсеров для Java: нужно поддерживать оба.
- Файлы с `PARTIAL_PARSED` дают неполный граф — учитывать при интерпретации IoU.
- При добавлении нового языка достаточно добавить grammar в tree-sitter.
- Shadow pipeline добавляет ~20-30% времени парсинга (дополнительный проход + классификация).
