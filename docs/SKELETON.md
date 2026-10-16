# Структура скелета — справочник (trainticket.json.gz)

> Данные получены прямым осмотром графа. Обновлять при обнаружении новых паттернов.

---

## Статистика

| Вид | Кол-во | Примечание |
|---|---|---|
| **nodes** | **15 229** | |
| method | 1 538 | из них abstract=342, unresolved=18 |
| call_site | 8 961 | ~6 вызовов на метод |
| parameter | 2 176 | |
| field | 1 066 | |
| type | 512 | классы, интерфейсы, enum |
| config_entry | 459 | ключи из application.yml |
| file | 442 | |
| module | 42 | сервисы + ts-common |
| data_resource | 33 | таблицы БД |

| Тип ребра | Кол-во | Для чего |
|---|---|---|
| declares | 11 676 | type→method, type→field |
| has_parameter | 2 176 | method→parameter |
| contains | 1 302 | module→file, file→type |
| **calls** | **1 240** | call_site→method |
| references_type | 1 084 | field/param→type |
| creates | 749 | call_site→type |
| **remote_call** | **415** | call_site→handler (кросс-сервис) |
| **data_access** | **319** | call_site→data_resource |
| **runtime_dispatch** | **287** | call_site→impl (CHA) |
| overrides | 240 | impl→interface-method |
| depends_on | 40 | module→module (всё → ts-common) |
| implements | 40 | impl-type→interface-type |
| message_delivery | 4 | RabbitMQ send→receive |
| extends | 1 | |

**Рёбра не имеют дополнительных полей** — только `source`, `target`, `type`. Нет `cfg_condition`, нет весов.

---

## Поля узлов по виду

### `method`
```
kind, owner (→type), name, descriptor (FQN параметров через запятую),
params_as_written (short names), return, return_raw,
modifiers, annotations [{name, args}], abstract, unresolved,
module, file, line, end
```

**Ключевые поля:**
- `annotations` — содержит `@*Mapping` с `args: (path = "/...")` → HTTP-путь уже в скелете
- `descriptor` — FQN типов параметров (как в NodeId)
- `owner` → type NodeId → аннотации класса (@RequestMapping базовый путь)
- `abstract: True` → не включать в executes напрямую, нужна CHA через overrides

### `call_site`
```
kind, method (→ вызывающий метод), callee (имя вызываемого),
receiver, receiver_type, arg_count, args_text (полный текст),
string_args [строковые литералы из аргументов], method_ref,
module, file, line, col
```

**Ключевые поля:**
- `method` — вызывающий метод (источник `calls` в ответе)
- `string_args` — строковые литералы аргументов, включая URL-фрагменты
- `receiver_type` — `RestTemplate` / `FeignClient` / имя интерфейса
- `callee` — имя вызываемого метода (короткое)

**Пример call_site для remote_call:**
```json
{
  "callee": "exchange",
  "receiver_type": "RestTemplate",
  "string_args": ["/api/v1/orderOtherService/orderOther/"],
  "method": "method:ts-execute-service:execute.serivce.ExecuteServiceImpl#getOrderByIdFromOrderOther(...)"
}
```

### `type`
```
kind, fqn, type_kind, modifiers, annotations [{name, args}],
type_params, extends_raw, implements_raw, module, file, line, end,
overload_collisions
```
- `annotations` содержит `@RequestMapping ("/api/v1/...")` — базовый путь класса

### `module`
```
kind, name, path, artifact_id, deployment_units
```
- `deployment_units` — список сервисов, в которых деплоится модуль
- `ts-common` → `deployment_units` = все 41 сервис (вхожден в каждый)

### `config_entry`
```
kind, module, key, value
```
- `key`: `server.port`, `spring.application.name`, `spring.cloud.nacos.discovery.server-addr`
- Значения могут содержать `${VAR:default}` Spring EL

### `data_resource`
```
kind, (id начинается с res:table:SERVICE.tablename)
```
Все 33 таблицы известны заранее — список фиксирован.

---

## Ключевые факты для предиктора

### Entry point detection
- **262 handler-метода** с @GetMapping/@PostMapping/@*Mapping
- HTTP-путь = `class @RequestMapping args` + `method @*Mapping args`
- baseline `Index.ann_paths()` уже парсит эти аннотации корректно
- Для шагов сценария — дополнить BM25 корпус logger-тегами из исходников

### BFS обход
- `calls`: через call_site.method → target method
- `runtime_dispatch`: call_site → impl через CHA (уже в скелете)
- `overrides`: для поиска конкретных реализаций при abstract методах
- `ts-common` методы доступны из любого сервиса (deployment_units = all)

### Remote prediction
- Скелет уже разрешил `restTemplate.exchange(url)` → handler-метод
- `call_site.string_args` содержит URL-фрагмент (помогает верифицировать)
- Задача: определить, активирован ли данный call_site в сценарии (→ branch selection)

### Branch selection
- Условий ветвления в скелете **нет** — только в исходниках через tree-sitter
- Рёбра плоские (`extra fields: {}`), нет `cfg_condition`
- Abstract методов 342 → всегда нужна CHA для поиска реализации

### Чего нет в скелете (нужно добирать)
| Отсутствует | Источник |
|---|---|
| Условия if/else | tree-sitter AST по `targets/` |
| UI-контекст кнопок | `ts-ui-dashboard/` JS/HTML |
| Logger-теги и flow-comments | Java исходники |
| Тела методов | Java исходники (только для условий) |
