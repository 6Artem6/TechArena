# ADR-026: Анализ байт-кода .jar/.class — возможности и ограничения

**Статус:** Принято (Tier 4 — если есть время)  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-021, ADR-024

---

## Контекст

Проверка train-ticket показала: `.jar`-файлы в репо — только `maven-wrapper.jar` (5 штук, одинаковые, MD5=`0d96a88e602e02418e0f838dea8e4dda`). Это инструментальный файл Maven для загрузки дистрибутива, не код сервисов. Скомпилированных сервисных JAR в репо нет — они были бы в `target/`, которая исключена из git.

---

## A. Что есть в репо

| Файл | Где | Что это | Ценность |
|---|---|---|---|
| `maven-wrapper.jar` | `.mvn/wrapper/` в 5 сервисах | Maven build tool (идентичны) | Нуль — не код проекта |
| `*.class` | Нигде вне `target/` | — | — |
| `*.jar` сервисов | Только в `target/` после `mvn package` | Скомпилированный сервис | ВЫСОКАЯ, если доступны |

---

## B. Что можно извлечь из сервисного .class / .jar

Инструмент: **`jawa` 2.2.0** — pure Python, без JVM. Установлен: `pip install jawa`.

```python
from jawa.cf import ClassFile
from io import BytesIO
import zipfile

def analyze_jar(jar_path: Path) -> ServiceBytecodeInfo:
    results = []
    with zipfile.ZipFile(jar_path) as z:
        for name in z.namelist():
            if not name.endswith('.class') or '$' in name:
                continue
            cf = ClassFile(BytesIO(z.read(name)))
            results.append(extract_class_info(cf))
    return results

def extract_class_info(cf: ClassFile) -> ClassBytecodeInfo:
    return ClassBytecodeInfo(
        fqn=cf.this.name.value.replace('/', '.'),
        super_fqn=cf.super_.name.value.replace('/', '.'),
        methods=[m.name.value for m in cf.methods],
        # Строки из constant pool
        string_constants=[
            c.string.value for c in cf.constants
            if hasattr(c, 'string')
        ],
    )
```

### Что извлекается:

| Данные | Ценность для blast radius | Примечание |
|---|---|---|
| FQN класса и суперкласса | Подтверждение IMPLEMENTS/EXTENDS рёбер | Уже есть из source |
| Все методы с JVM-дескриптором | Точные сигнатуры без шаблонов | Дополняет source |
| **Строки constant pool** | URL, queue names, @Value ключи | **Главная ценность** |
| Ссылки на другие классы (pool) | FQN без import-резолвинга | Дополняет SymbolIndex |

### Что НЕ извлекается:

- Имена локальных переменных (стираются при компиляции без `-g`)
- Комментарии (полностью исчезают)
- Generic-типы (type erasure: `List<Order>` → `List`)
- Шаблоны Spring (аннотации сохраняются, но их обработка — runtime)

### Ключевое преимущество: строки constant pool

В `.class` файле компилятор помещает в constant pool **все** строковые литералы, включая те, что в source-коде собираются конкатенацией компилятором (compile-time constants):

```java
// Source:
private static final String BASE = "/api/v1/";
private static final String URL = BASE + "orderservice/order";
// → в constant pool: "/api/v1/orderservice/order"  (уже склеено!)
```

Это позволяет получить готовые URL без разворачивания цепочек присваиваний (Value Flow Graph).

---

## C. Практическое применение (если JARы доступны)

```python
def enrich_from_bytecode(
    service_name: str,
    jar_path: Path,
    symbol_index: SymbolIndex,
    route_index: RouteIndex,
) -> None:
    """
    Опциональное обогащение графа данными из скомпилированного JAR.
    Вызывается только если jar_path существует; не заменяет source-анализ.
    """
    for cls_info in analyze_jar(jar_path):
        for s in cls_info.string_constants:
            # 1. URL-константы → RouteIndex
            if re.match(r'/api/v\d+/', s):
                route_index.add_hint(s, source='bytecode', service=service_name)
            
            # 2. Имена очередей → верификация SymbolIndex
            elif 5 < len(s) < 50 and s.islower() and '_' in s:
                symbol_index.verify_queue_name(s, service=service_name)
            
            # 3. Сервисные имена ts-*
            elif s.startswith('ts-') and '-service' in s:
                route_index.add_service_ref(s, service=service_name, source='bytecode')
```

### Где найти JAR после сборки:

```
target/<service-name>-<version>.jar
# Пример:
ts-notification-service/target/ts-notification-service-1.0.0.jar
```

### Как получить без Maven/интернета (если нужно):

```bash
# Собрать один сервис локально (только если нужно):
# cd ts-notification-service && ./mvnw package -DskipTests -o
# -o = offline mode (использует ~/.m2/repository)
```

---

## D. Приоритет и решение

**Решение: не реализовывать в MVP, добавить как опциональное обогащение.**

Причины:
1. Сервисных JAR в репо нет — потребуется сборка
2. Все нужные данные уже извлекаются из source через tree-sitter + Value Flow Graph
3. Bytecode analysis = верификация, а не новые рёбра

**Единственный реальный плюс**: готовые склеенные URL из compile-time constant folding. Но это редкий случай — в train-ticket URL строятся через `getServiceUrl()` + конкатенацию в runtime, не через static final.

**Флаг**: `--bytecode-hints <target-dir>` — если директория существует, обогатить RouteIndex строками из JAR.

```
Tier 1-3: source-анализ (tree-sitter, Value Flow Graph, SymbolIndex)
Tier 4:   --bytecode-hints → jawa constant pool extraction
```
