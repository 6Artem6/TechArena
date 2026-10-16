# ADR-029: PackageIndex, полиглот URL-резолвинг, интерфейсы без реализаций

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-001, ADR-022, ADR-024, ADR-026

---

## Контекст

Проверка реального кода train-ticket выявила четыре паттерна, которые не покрыты предыдущими ADR:

1. **Коллизия пакетных префиксов** — два сервиса используют `fdse.microservice.*`
2. **Python/Node.js URL с `os.getenv()`** — хардкодированный дефолт + опциональный env-override
3. **Spring Data репозитории** — интерфейс без реализации, генерируемой прокси
4. **`edu.fudan.common.*`** — разделяемая библиотека (не сервис), нужен тип LIBRARY

---

## A. PackageIndex — маппинг пакетного префикса в сервис

### A.1 Как строится (программно)

```python
JAVA_SOURCE_ROOT_RE = re.compile(r'src/main/java/')

def build_package_index(repo_root: Path) -> dict[str, list[str]]:
    """package_prefix → [service_dir, ...]. Один запуск при индексации."""
    index: dict[str, list[str]] = defaultdict(list)

    for svc_dir in repo_root.iterdir():
        if not svc_dir.is_dir():
            continue
        java_root = svc_dir / 'src' / 'main' / 'java'
        if not java_root.exists():
            continue
        # Корневой пакет = первый сегмент из любого package-declaration
        for jf in java_root.rglob('*.java'):
            try:
                line = next(
                    l for l in jf.read_text(errors='replace').splitlines()
                    if l.startswith('package ')
                )
            except StopIteration:
                continue
            prefix = line.split()[1].rstrip(';').split('.')[0]
            svc_name = svc_dir.name
            if svc_name not in index[prefix]:
                index[prefix].append(svc_name)
            break  # одного файла достаточно для определения корневого пакета

    return dict(index)
```

### A.2 Полная таблица для train-ticket (результат программного сбора)

| Пакетный префикс | Сервис | Примечание |
|---|---|---|
| `adminbasic` | `ts-admin-basic-info-service` | |
| `adminorder` | `ts-admin-order-service` | |
| `adminroute` | `ts-admin-route-service` | |
| `admintravel` | `ts-admin-travel-service` | |
| `adminuser` | `ts-admin-user-service` | |
| `assurance` | `ts-assurance-service` | |
| `auth` | `ts-auth-service` | |
| `cancel` | `ts-cancel-service` | |
| `com` | `ts-payment-service` | `com.trainticket.*` |
| `config` | `ts-config-service` | |
| `consign` | `ts-consign-service` | |
| `consignprice` | `ts-consign-price-service` | |
| `contacts` | `ts-contacts-service` | |
| `delivery` | `ts-delivery-service` | |
| `edu` | `ts-common` | **LIBRARY**, не SERVICE |
| `execute` | `ts-execute-service` | |
| `fdse` | `ts-basic-service`, `ts-station-service` | **КОЛЛИЗИЯ** |
| `food` | `ts-station-food-service` | |
| `food_delivery` | `ts-food-delivery-service` | |
| `foodsearch` | `ts-food-service` | |
| `gateway` | `ts-gateway-service` | |
| `inside_payment` | `ts-inside-payment-service` | |
| `notification` | `ts-notification-service` | |
| `order` | `ts-order-service` | |
| `other` | `ts-order-other-service` | |
| `plan` | `ts-route-plan-service` | |
| `preserve` | `ts-preserve-service` | |
| `preserveOther` | `ts-preserve-other-service` | |
| `price` | `ts-price-service` | |
| `rebook` | `ts-rebook-service` | |
| `route` | `ts-route-service` | |
| `seat` | `ts-seat-service` | |
| `security` | `ts-security-service` | |
| `train` | `ts-train-service` | |
| `trainFood` | `ts-train-food-service` | |
| `travel` | `ts-travel-service` | |
| `travel2` | `ts-travel2-service` | |
| `travelplan` | `ts-travel-plan-service` | |
| `user` | `ts-user-service` | |
| `verifycode` | `ts-verification-code-service` | |
| `waitorder` | `ts-wait-order-service` | |

### A.3 Коллизия `fdse.microservice.*`

`ts-basic-service` и `ts-station-service` оба используют пакет `fdse.microservice.*`:

```
ts-basic-service/src/main/java/fdse/microservice/
    config/, controller/, service/       (нет entity, repository)

ts-station-service/src/main/java/fdse/microservice/
    config/, controller/, entity/, init/, repository/, service/
```

**Разрешение по субпакету:**

```python
FDSE_DISAMBIGUATE = {
    'fdse.microservice.entity':     'ts-station-service',
    'fdse.microservice.repository': 'ts-station-service',
    'fdse.microservice.init':       'ts-station-service',
    # controller, service, config — ambiguous → оба
}

def resolve_package_to_service(
    import_fqn: str,
    caller_service: str,
    package_index: dict[str, list[str]],
) -> list[str]:
    """Возвращает список сервисов для данного import FQN."""
    prefix = import_fqn.split('.')[0]
    candidates = package_index.get(prefix, [])

    if len(candidates) <= 1:
        return candidates

    # Коллизия: пробуем уточнить по субпакету
    for known_subpkg, svc in FDSE_DISAMBIGUATE.items():
        if import_fqn.startswith(known_subpkg):
            return [svc]

    # Исключаем собственный пакет вызывающего (import внутри своего сервиса)
    external = [c for c in candidates if c != caller_service]
    return external if external else candidates
```

**Важно:** `FDSE_DISAMBIGUATE` не хардкодит имена произвольно — он строится тем же алгоритмом: сканировать субдиректории `fdse.microservice/` в каждом сервисе и посмотреть, у кого есть `entity/`, у кого нет.

```python
def build_fdse_disambiguate(repo_root: Path, candidates: list[str]) -> dict[str, str]:
    """Строит disambiguation-таблицу для коллизионного пакета."""
    result = {}
    for svc_name in candidates:
        svc_java = repo_root / svc_name / 'src' / 'main' / 'java'
        for sub in svc_java.rglob('*'):
            if sub.is_dir():
                pkg_path = str(sub.relative_to(svc_java)).replace('/', '.')
                result[pkg_path] = svc_name
    return result
```

### A.4 `edu.fudan.common.*` — библиотека, не сервис

`ts-common` — не deployable сервис (не в docker-compose `services:`). Это Maven-зависимость.

```python
LIBRARY_PACKAGES = {'edu.fudan.common'}  # строится: пакеты без docker-compose entry

def classify_import(
    fqn: str,
    service_registry: set[str],
    package_index: dict[str, list[str]],
) -> str:
    """'service' | 'library' | 'external' | 'self'"""
    prefix = fqn.split('.')[0]
    candidates = package_index.get(prefix, [])
    if not candidates:
        return 'external'  # java.*, org.springframework.*, com.alibaba.*
    in_registry = [c for c in candidates if c in service_registry]
    if not in_registry:
        return 'library'   # ts-common → edu.fudan.common.*
    return 'service'
```

`import edu.fudan.common.entity.Order` → тип `library` → `USES_DTO` ребро с атрибутом `source=ts-common`.

---

## B. Python и Node.js: URL-резолвинг через `os.getenv()`

### B.1 Паттерн в ts-voucher-service

```python
# server.py
order_url = 'http://ts-order-service:12031'        # дефолт
order_other_url = 'http://ts-order-other-service:12032'
if os.getenv("ORDER_SERVICE_URL") is not None:
    order_url = os.getenv("ORDER_SERVICE_URL")     # override из env
if os.getenv("ORDER_OTHER_SERVICE_URL") is not None:
    order_other_url = os.getenv("ORDER_OTHER_SERVICE_URL")
```

**Верификация в docker-compose:** секция `environment:` в `ts-voucher-service` в quickstart-docker-compose.yml **пуста** → env-переменные не выставлены → дефолт `http://ts-order-service:12031` является финальным значением.

### B.2 Алгоритм резолвинга

```python
PYTHON_URL_LITERAL_RE = re.compile(
    r"""['"]https?://([a-z0-9-]+)(?::\d+)?['"]"""
)
PYTHON_GETENV_RE = re.compile(
    r"""os\.getenv\s*\(\s*['"]([^'"]+)['"]\s*\)"""
)

@dataclass
class PythonServiceCall:
    host: str          # "ts-order-service"
    env_var: str       # "ORDER_SERVICE_URL" или None
    resolved: str      # финальный хост

def extract_python_service_urls(
    py_file: Path,
    compose_env: dict[str, str],     # из docker-compose environment:
) -> list[PythonServiceCall]:
    text = py_file.read_text(errors='replace')
    calls = []

    # Ищем паттерн: url_var = 'http://ts-xxx:PORT' + if getenv(...) override
    for m_default in PYTHON_URL_LITERAL_RE.finditer(text):
        host = m_default.group(1)
        if not re.match(r'^ts-', host):
            continue

        # Ищем getenv в окне после дефолта (200 символов)
        window = text[m_default.start():m_default.start() + 400]
        m_env = PYTHON_GETENV_RE.search(window)
        env_key = m_env.group(1) if m_env else None

        # Резолвинг: env из compose > дефолт
        if env_key and env_key in compose_env:
            env_val = compose_env[env_key]
            resolved_host = PYTHON_URL_LITERAL_RE.search(env_val)
            final_host = resolved_host.group(1) if resolved_host else host
        else:
            final_host = host

        calls.append(PythonServiceCall(
            host=host, env_var=env_key, resolved=final_host,
        ))
    return calls
```

### B.3 Node.js `process.env` (ts-ticket-office-service)

`ts-ticket-office-service` использует `process.env` только для БД-параметров (`TICKET_OFFICE_MYSQL_*`). Вызовов к другим ts-сервисам нет. Сервис — чистый CRUD поверх MySQL, порт 16108.

### B.4 Go `ts-news-service`

Весь сетевой код закомментирован (`// //` паттерн из ADR-025). Единственный активный код:
```go
web.Get("/(.*)", hello)
web.Run("0.0.0.0:12862")
```
Возвращает статический JSON. Никаких вызовов к другим сервисам. Порт 12862.

---

## C. Интерфейсы и реализации в Java

### C.1 Таблица типов узлов по ключевым словам

| Ключевые слова в файле | NODE_TYPE | Есть ли реализация? |
|---|---|---|
| `public interface X` (без extends) | `INTERFACE` | Да, `XImpl` |
| `public interface X extends CrudRepository` | `REPOSITORY` | **Нет** — Spring-прокси |
| `public interface X extends JpaRepository` | `REPOSITORY` | **Нет** |
| `public interface X extends MongoRepository` | `REPOSITORY` | **Нет** |
| `public interface X extends ReactiveMongoRepository` | `REPOSITORY` | **Нет** |
| `public abstract class X` | `ABSTRACT_CLASS` | Да, подклассы |
| `@RestController` class X | `CLASS` + controller-role | |
| `@Service` class X | `CLASS` + service-role | |
| `class XImpl implements X` | `CLASS` → IMPLEMENTS → INTERFACE | |

**Счётчики в train-ticket:**
- 86 интерфейсов (`public interface`)
- 27 Spring Data репозиториев (`extends CrudRepository/JpaRepository/MongoRepository`)
- 121 класс с `implements`

### C.2 Детектор типа

```python
SPRING_REPO_RE = re.compile(
    r'extends\s+(?:CrudRepository|JpaRepository|MongoRepository|'
    r'ReactiveMongoRepository|PagingAndSortingRepository)\s*[<{]',
)
INTERFACE_RE = re.compile(r'\binterface\s+\w+')
ABSTRACT_CLASS_RE = re.compile(r'\babstract\s+class\s+\w+')
IMPLEMENTS_RE = re.compile(r'\bclass\s+(\w+)\s+implements\s+([\w,\s]+)')

def classify_java_type(source: str) -> str:
    if SPRING_REPO_RE.search(source):
        return 'REPOSITORY'   # не искать реализацию
    if INTERFACE_RE.search(source):
        return 'INTERFACE'    # искать XImpl
    if ABSTRACT_CLASS_RE.search(source):
        return 'ABSTRACT_CLASS'
    return 'CLASS'
```

### C.3 Почему важно не искать реализацию репозитория

`ContactsRepository extends CrudRepository` — Spring Data генерирует прокси в рантайме. Файла `ContactsRepositoryImpl.java` не существует. Если искать реализацию — будет ошибка "не найдено". Правильное поведение: тип `REPOSITORY` → не строить `CONTAINS` до несуществующего `Impl`.

---

## D. Кросс-сервисные импорты: только через ts-common

### D.1 Что нашли

Прямых импортов пакетов одного сервиса в другой **не обнаружено**. Все кросс-сервисные зависимости идут через `edu.fudan.common.*` (ts-common):

```
edu.fudan.common.entity.Order, Route, Seat, Contacts, TrainType, ...
edu.fudan.common.util.Response
edu.fudan.common.security.jwt.JWTFilter
```

### D.2 Построение USES_DTO рёбер из импортов

```python
def import_to_edges(
    import_fqn: str,
    caller_service: str,
    package_index: dict[str, list[str]],
    service_registry: set[str],
) -> list[tuple[str, str, dict]]:
    kind = classify_import(import_fqn, service_registry, package_index)

    if kind == 'external':
        return []  # java.*, org.springframework.* — не интересуют

    if kind == 'library':
        # edu.fudan.common.entity.X → USES_DTO к ts-common
        class_name = import_fqn.split('.')[-1].rstrip('*')
        return [(caller_service, f'ts-common::{class_name}',
                 {'type': 'USES_DTO', 'source': 'ts-common'})]

    if kind == 'service':
        targets = resolve_package_to_service(
            import_fqn, caller_service, package_index)
        return [
            (caller_service, tgt, {'type': 'USES_DTO', 'source': 'import'})
            for tgt in targets if tgt != caller_service
        ]
    return []
```

---

## E. Итоговый список паттернов для парсера

| Паттерн | Файл/язык | Действие парсера |
|---|---|---|
| `order_url = 'http://ts-xxx:PORT'` + `os.getenv(...)` | Python | Извлечь host из литерала, проверить compose-env |
| `process.env.MYSQL_*` | Node.js | Только БД-параметры → SHARES_RESOURCE, не CALLS_SERVICE |
| `web.Run("0.0.0.0:12862")` | Go | Порт сервиса, вызовов нет |
| `import edu.fudan.common.entity.X` | Java | USES_DTO → ts-common, тип library |
| `import fdse.microservice.entity.*` | Java | Disambiguate → ts-station-service |
| `import fdse.microservice.service.*` | Java | Ambiguous → check caller context |
| `extends CrudRepository<T, ID>` | Java | NODE_TYPE=REPOSITORY, не искать Impl |
| `class XImpl implements X` | Java | NODE_TYPE=CLASS + IMPLEMENTS ребро |
| `image: ${NAMESPACE}/ts-xxx:${TAG}` | docker-compose | Имя сервиса из YAML-ключа, не из image |
