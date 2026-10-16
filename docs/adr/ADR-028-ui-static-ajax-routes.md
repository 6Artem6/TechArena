# ADR-028: Статика UI как источник RouteIndex — AJAX/HTTP вызовы

**Статус:** Принято  
**Дата:** 2026-10-13  
**Связанные:** ADR-011, ADR-025, ADR-027

---

## Контекст

`ts-ui-dashboard` — статический nginx-сервер. Его JS-файлы содержат все URL которые реально вызываются пользователем браузера. Это независимый от кода сервисов источник для RouteIndex.

**nginx.conf единственный proxy_pass:**
```nginx
location /api/v1/ {
    proxy_pass http://ts-gateway-service:18888;
}
```
Всё что начинается с `/api/v1/` → идёт через gateway → сервис. Остальные пути → статика.

---

## A. Два типа JS-файлов — два разных паттерна вызовов

### A.1 Angular-файлы (`assets/js/*.js`) — `/api/v1/` через gateway

Используют AngularJS `$http` сервис. URL — полные, начинаются с `/api/v1/`:

```javascript
// admin_config.js
$http({ method: "get", url: "/api/v1/adminbasicservice/adminbasic/configs" })
$http({ method: "delete", url: "/api/v1/adminbasicservice/adminbasic/configs/" + config.name })
$http({ method: "put", url: "/api/v1/adminbasicservice/adminbasic/configs" })
$http({ method: "post", url: "/api/v1/adminbasicservice/adminbasic/configs" })
```

**Всего найдено 49 уникальных `/api/v1/` URL из 15 файлов** → 27 сервисов вызывается из UI.

### A.2 jQuery-файлы (`js/*.js`) — относительные пути без `/api/v1/`

Используют jQuery `$.ajax` с короткими путями:

```javascript
// myflow.js, flowPreserve.js
url: "/cancelOrder"       url: "/cancelCalculateRefund"
url: "/station/queryById" url: "/inside_payment/pay"
url: "/food/getFood"      url: "/rebook/rebook"
url: "/office/addOffice"  url: "/account/findAll"
```

Эти пути **не попадают** в `location /api/v1/` nginx. Возможные интерпретации:
1. **Legacy код** — старый API до перехода на `/api/v1/`; файлы не удалены
2. **Обработка в Node.js** — `ts-ticket-office-service` (Node.js, порт 12306) может обслуживать часть этих путей
3. **Прямые вызовы** — если эти страницы открываются на другом порту

**Решение:** извлекать URL из `js/` файлов только для RouteIndex hint с `source=legacy_ui`, confidence=0.5. Не создавать CALLS_SERVICE рёбра от UI к сервисам на основе этих путей.

---

## B. Маппинг UI → сервисы (по Angular-файлам)

### B.1 Полная таблица: 27 сервисов, 49 URL

| Сервис | URL-паттерны | UI-файлы |
|---|---|---|
| `ts-admin-basic-info-service` | `/api/v1/adminbasicservice/adminbasic/{configs,contacts,prices,stations,trains}` | admin_*.js (5 файлов) |
| `ts-admin-order-service` | `/api/v1/adminorderservice/adminorder` | old_index.js |
| `ts-admin-route-service` | `/api/v1/adminrouteservice/adminroute` | admin_route.js |
| `ts-admin-travel-service` | `/api/v1/admintravelservice/admintravel` | admin_travel.js |
| `ts-admin-user-service` | `/api/v1/adminuserservice/users` | admin_user.js |
| `ts-assurance-service` | `/api/v1/assuranceservice/assurances/types` | client_ticket_book.js |
| `ts-auth-service` | `/api/v1/users/login` | adminlogin.js, client_login.js |
| `ts-avatar-service` | `/api/v1/avatar` | upload.js |
| `ts-cancel-service` | `/api/v1/cancelservice/cancel/{,refound/}` | client_order_list.js |
| `ts-consign-service` | `/api/v1/consignservice/consigns/{,account/,order/}` | client_consign_list.js |
| `ts-contacts-service` | `/api/v1/contactservice/contacts{,/account/}` | client_ticket_book.js |
| `ts-execute-service` | `/api/v1/executeservice/execute/{collected/,execute/}` | client_collect.js |
| `ts-food-service` | `/api/v1/foodservice/foods/` | client_ticket_book.js |
| `ts-inside-payment-service` | `/api/v1/inside_pay_service/inside_payment` | client_order_list.js |
| `ts-order-other-service` | `/api/v1/orderOtherService/orderOther/refresh` | client_collect.js, client_order_list.js |
| `ts-order-service` | `/api/v1/orderservice/order/refresh` | client_collect.js, client_order_list.js |
| `ts-preserve-other-service` | `/api/v1/preserveotherservice/preserveOther` | client_ticket_book.js |
| `ts-preserve-service` | `/api/v1/preserveservice/preserve` | client_ticket_book.js |
| `ts-rebook-service` | `/api/v1/rebookservice/rebook{,/difference}` | client_order_list.js, orderRebook.js |
| `ts-route-service` | `/api/v1/routeservice/routes` | admin_travel.js |
| `ts-station-service` | `/api/v1/stationservice/stations{/name/}` | admin_travel.js, client_order_list.js |
| `ts-train-service` | `/api/v1/trainservice/trains` | admin_travel.js |
| `ts-travel-plan-service` | `/api/v1/travelplanservice/travelPlan/{cheapest,minStation,quickest}` | client_adsearch.js |
| `ts-travel-service` | `/api/v1/travelservice/trips/left` | client_order_list.js, index.js |
| `ts-travel2-service` | `/api/v1/travel2service/trips/left` | client_order_list.js, index.js |
| `ts-user-service` | `/api/v1/userservice/users` | old_index.js |
| `ts-verification-code-service` | `/api/v1/verifycode/generate` | client_login.js |

### B.2 Сервисы НЕ вызываемые из UI напрямую (14 штук)

Это backend-internal сервисы — вызываются только из других сервисов:

```
ts-basic-service         — базовые данные (вызывает admin-basic-info-service)
ts-config-service        — runtime config
ts-consign-price-service — цена доставки (вызывает consign-service)
ts-food-map-service      — карта еды (вызывает food-service)
ts-news-service          — новости (Go, заглушка)
ts-notification-service  — email/SMS уведомления (RabbitMQ)
ts-payment-service       — платёж (вызывает inside-payment)
ts-price-service         — цена билета (вызывает seat/travel)
ts-route-plan-service    — план маршрута (вызывает route/station/travel)
ts-seat-service          — мест (вызывает order/travel)
ts-security-service      — безопасность заказа (вызывает order)
ts-ticket-office-service — кассы (Node.js, порт 12306)
ts-ticketinfo-service    — инфо о билете
ts-voucher-service       — ваучеры (Python, вызывает order)
```

---

## C. Алгоритм парсинга JS

```python
# Два паттерна + трассировка переменных
ANGULAR_HTTP_RE = re.compile(
    r'\$http\s*\(\s*\{[^}]*?url\s*:\s*["\']([^"\']+)["\']',
    re.DOTALL,
)
ANGULAR_URL_CONCAT_RE = re.compile(
    r'url\s*:\s*["\'](/api/v\d+/[^"\']+)["\']',
)
URL_VAR_RE = re.compile(
    r'(?:var|let|const)\s+(\w+)\s*=\s*["\']([^"\']+)["\']'
)

def extract_ui_routes(js_file: Path) -> list[UIRoute]:
    text = js_file.read_text(errors='replace')
    routes = []
    
    # Статические URL
    for m in ANGULAR_URL_CONCAT_RE.finditer(text):
        url = m.group(1).rstrip('/')
        if not url.startswith('/api/'):
            continue
        method = infer_http_method(text, m.start())
        routes.append(UIRoute(
            url=url,
            method=method,
            file=str(js_file),
            source='angular_static',
            confidence=0.95,
        ))
    
    # Динамические: "/api/v1/service/" + variable → шаблон
    for m in re.finditer(r'url\s*:\s*["\']([^"\']+)["\'][^,}]*\+', text):
        url_prefix = m.group(1)
        if url_prefix.startswith('/api/'):
            routes.append(UIRoute(
                url=url_prefix + '{id}',
                method='UNKNOWN',
                file=str(js_file),
                source='angular_dynamic',
                confidence=0.80,
            ))
    
    return routes

def infer_http_method(text: str, pos: int) -> str:
    """Ищем method: "get/post/put/delete" рядом с url."""
    window = text[max(0, pos-200):pos+50]
    m = re.search(r'method\s*:\s*["\'](\w+)["\']', window, re.IGNORECASE)
    return m.group(1).upper() if m else 'GET'
```

---

## D. Ценность для RouteIndex

```
Приоритет RouteIndex (обновлённый):
  1. gateway.yml (service discovery — авторитетный)
  2. @RequestMapping в main-коде (что объявлено)
  3. UI Angular-вызовы (что реально используется, confidence=0.95)
  4. MockMvc тест-хинты (confidence=0.80)
  5. Legacy UI jQuery (confidence=0.50 — может быть устаревшим)
```

**Почему UI-источник ценен:**
- Показывает **реально используемые** endpoints (не просто объявленные)
- Прямая связь `ts-ui-dashboard → CALLS_SERVICE → ts-xxx` без gateway как посредника
- Обнаруживает `ts-avatar-service` — Python-сервис без getServiceUrl вызовов, но явно вызывается из upload.js
- Обнаруживает pattern: `ts-order-service` и `ts-order-other-service` вызываются **из одного файла** (`client_order_list.js`, `client_collect.js`) — подтверждение SIBLING_OF

---

## E. CALLS_SERVICE рёбра от ts-ui-dashboard

```python
# ts-ui-dashboard — это SERVICE-узел в графе
# Рёбра: ts-ui-dashboard → CALLS_SERVICE → ts-xxx для каждого UI-вызова
# Источник: UI-парсер, confidence из UIRoute

def add_ui_service_edges(
    G: nx.MultiDiGraph,
    ui_routes: list[UIRoute],
    segment_to_service: dict[str, str],
) -> None:
    for route in ui_routes:
        parts = route.url.strip('/').split('/')
        # /api/v1/<segment>/...
        segment = parts[2] if len(parts) > 2 else None
        svc = segment_to_service.get(segment)
        if svc:
            G.add_edge(
                'ts-ui-dashboard',
                svc,
                type='CALLS_SERVICE',
                method=route.method,
                path=route.url,
                conf=route.confidence,
                source=route.source,
            )
```

**Как строится `segment_to_service` — без хардкода:**

```python
GATEWAY_ROUTE_RE = re.compile(
    r'uri\s*:\s*(lb://|http://)?(?P<svc>ts-[\w-]+)',
    re.IGNORECASE,
)
REQUEST_MAPPING_RE = re.compile(
    r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
)

def build_segment_to_service(
    gateway_yaml_paths: list[Path],
    java_files: list[Path],
) -> dict[str, str]:
    """Строит маппинг URL-сегмента → имя сервиса из gateway.yml + аннотаций."""
    result: dict[str, str] = {}

    # Источник 1: gateway YAML — routes[].uri и routes[].predicates Path=
    PATH_PRED_RE = re.compile(r'Path\s*=\s*/api/v\d+/([^/,\s]+)')
    for gw in gateway_yaml_paths:
        text = gw.read_text(errors='replace')
        segs = PATH_PRED_RE.findall(text)
        svcs = GATEWAY_ROUTE_RE.findall(text)
        for seg, (_, svc) in zip(segs, svcs):
            result[seg.lower()] = svc

    # Источник 2: @RequestMapping на классах контроллеров
    for jf in java_files:
        if '/test/' in str(jf):
            continue
        text = jf.read_text(errors='replace')
        for m in REQUEST_MAPPING_RE.finditer(text):
            path = m.group(1)  # "/api/v1/orderservice"
            parts = path.strip('/').split('/')
            # /api/v1/<segment> — берём третий сегмент
            if len(parts) >= 3 and parts[0] == 'api':
                seg = parts[2].lower()
                # Имя сервиса — из пакета файла или имени директории
                svc = _service_name_from_path(jf)
                if svc:
                    result.setdefault(seg, svc)

    return result

def _service_name_from_path(java_file: Path) -> str | None:
    """ts-order-service/src/... → 'ts-order-service'."""
    for part in java_file.parts:
        if re.match(r'^ts-.+-service$', part):
            return part
    return None
```

**`segment_to_service` строится один раз при индексации.** Хардкод не нужен: сегмент `orderservice` из gateway.yml с `uri: lb://ts-order-service` даёт маппинг автоматически.

**Итог:** `ts-ui-dashboard` добавляется как полноценный SERVICE-узел с 27 исходящими CALLS_SERVICE рёбрами. Это важно: изменение в любом из 27 сервисов затрагивает UI-слой.

---

## F. readme.txt как источник legacy-маршрутов и техдолга

### F.1 `ts-ui-dashboard/readme.txt` — фрагмент старого nginx

Файл содержит **частичный nginx.conf** (докерная инфра до перехода на gateway). Не `.txt`-документация — это конфигурация старой архитектуры.

**Старые маршруты (без `/api/v1/`):**

| URL-путь | Сервис | Порт | Статус |
|---|---|---|---|
| `/login`, `/logout` | `ts-login-service` | 12342 | **МЁРТВЫЙ** (сервиса нет) |
| `/register` | `ts-register-service` | 12344 | **МЁРТВЫЙ** (сервиса нет) |
| `/verification/generate` | `ts-verification-code-service` | 15678 | Живой |
| `/station/{create,exist,delete}` | `ts-station-service` | 12345 | Живой |
| `/train/{create,retrieve,update,delete}` | `ts-train-service` | 14567 | Живой |
| `/config/{create,query,update,delete}` | `ts-config-service` | 15679 | Живой |
| `/cancelOrder`, `/saveOrderInfo`, `/queryOrders` | `ts-order-service` | 12031 | Живой |
| `/travel/{create,retrieve,update,delete,query}` | `ts-travel-service` | 12346 | Живой |
| `/createNewContacts`, `/deleteContacts` | `ts-contacts-service` | 12347 | Живой |

**Вывод:** jQuery `js/*.js` файлы вызывали ИМЕННО эти пути — они спроектированы под старый nginx. Текущий nginx.conf содержит только `/api/v1/ → gateway`. Это объясняет, почему jQuery-пути не попадают под `/api/v1/`.

**Эволюция архитектуры:**
```
Старая: Browser → nginx (legacy routes) → ts-xxx-service напрямую
Новая:  Browser → nginx (/api/v1/) → ts-gateway-service → ts-xxx-service
```

### F.2 `ts-contacts-service/readme.txt` — код с мёртвым SSO

```java
// Из readme.txt — это НЕ текущий код
restTemplate.getForObject(
    "http://ts-sso-travel.service:12349/verifyLoginToken/" + loginToken,
    VerifyResult.class);
```

`ts-sso-travel.service:12349` — **мёртвый сервис** (в репо не существует). Старое имя: `contacts-query-service:12339` (текущее: `ts-contacts-service:12347`). Смена имён — миграция на `ts-`-префикс.

### F.3 Стратегия парсинга readme.txt файлов

```python
README_NGINX_RE = re.compile(
    r'location\s+(/\S+)\s*\{[^}]*?proxy_pass\s+(https?://\S+?);\s*\}',
    re.DOTALL,
)

def parse_readme_file(path: Path) -> list[LegacyRoute]:
    text = path.read_text(errors='replace')

    # Пропускаем если только docker/build/k8s команды — нет proxy_pass
    if 'proxy_pass' not in text:
        return []

    routes = []
    for m in README_NGINX_RE.finditer(text):
        url_path = m.group(1)
        upstream = m.group(2)  # "http://ts-station-service:12345"
        host = upstream.split('//')[-1].split(':')[0]
        routes.append(LegacyRoute(
            url=url_path,
            upstream_host=host,
            source='legacy_nginx_readme',
            confidence=0.40,
        ))
    return routes
```

### F.4 Детектор техдолга: мёртвые сервисы в документации

Хардкодить имена нельзя — детектор должен работать на любом репо. Мёртвые сервисы выводятся сравнением двух множеств:

- **`service_registry`** — строится из docker-compose (секции `services:`) + обход директорий верхнего уровня по паттерну `ts-*`
- **`doc_hosts`** — имена хостов из `proxy_pass` в readme.txt

```python
@dataclass
class DeadDocRef:
    host: str           # "ts-login-service"
    url: str            # "/login"
    source: str         # "legacy_nginx_readme"

def build_service_registry(repo_root: Path) -> set[str]:
    """Авторитетное множество живых имён из docker-compose + директорий репо.

    Включает все имена: ts-xxx-service, ts-xxx-mysql, redis — это намеренно.
    Сравниваем хосты из readme.txt (proxy_pass) с этим множеством.
    Если хоста нет ни там ни там — он мёртвый. Лишние инфра-имена
    (redis, mysql) не создают ложных пропусков: их нет в proxy_pass.
    """
    names: set[str] = set()

    # Источник 1: docker-compose services: (авторитетный — что реально деплоится)
    for compose in repo_root.rglob('docker-compose*.yml'):
        if 'target' in compose.parts:
            continue
        try:
            data = yaml.safe_load(compose.read_text())
            for svc in (data or {}).get('services', {}):
                names.add(svc)
        except Exception:
            pass

    # Источник 2: директории верхнего уровня (что есть в репо)
    for d in repo_root.iterdir():
        if d.is_dir() and not d.name.startswith('.'):
            names.add(d.name)

    return names

def detect_dead_doc_refs(
    routes: list[LegacyRoute],
    service_registry: set[str],
) -> list[DeadDocRef]:
    """Хосты из readme.txt, которых нет в service_registry → техдолг."""
    dead = []
    for route in routes:
        if route.upstream_host not in service_registry:
            dead.append(DeadDocRef(
                host=route.upstream_host,
                url=route.url,
                source=route.source,
            ))
    return dead
```

**Почему это работает без хардкода:**
- `ts-login-service` → нет в docker-compose И нет директории → `dead_doc_ref`
- `ts-verification-code-service` → есть в docker-compose → живой
- `contacts-query-service` → нет → `dead_doc_ref`
- Новый репо с другими сервисами → логика та же, список имён не нужен

**tech_debt[] записи:** `detect_dead_doc_refs()` → `technical_debt[type=dead_doc_ref, source=readme.txt, host=<name>]`.

### F.5 Обновлённые приоритеты RouteIndex

```
1. gateway.yml                 (авторитетный, confidence=1.00)
2. @RequestMapping в main-коде (объявлено в коде, confidence=0.95)
3. UI Angular /api/v1/ вызовы  (реально используется, confidence=0.95)
4. MockMvc тест-хинты          (confidence=0.80)
5. Legacy jQuery (js/*.js)     (confidence=0.50 — вероятно устаревший)
6. readme.txt legacy nginx     (confidence=0.40 — старая архитектура, проверять)
7. readme.txt code snippets    (confidence=0.25 — контекст для техдолга)
```
