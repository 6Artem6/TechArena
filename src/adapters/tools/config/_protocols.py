"""Разбор URL по любому протоколу — общий модуль для всех конфиг-адаптеров.

Покрывает: http(s), ws(s), mysql, postgresql, mongodb, redis, amqp(s),
           jdbc:*, kafka, grpc, tcp, ftp, sftp, smtp(s), nats, zookeeper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..base import FindingKind

# ─────────────────────────────────────────────────────────────────────────────
# Таблица протоколов
# ─────────────────────────────────────────────────────────────────────────────

# protocol → (FindingKind, default_port, категория)
PROTOCOL_TABLE: dict[str, tuple[FindingKind, int, str]] = {
    "http":       (FindingKind.CALLS_SERVICE, 80,   "http"),
    "https":      (FindingKind.CALLS_SERVICE, 443,  "http"),
    "ws":         (FindingKind.CALLS_SERVICE, 80,   "websocket"),
    "wss":        (FindingKind.CALLS_SERVICE, 443,  "websocket"),
    "grpc":       (FindingKind.CALLS_SERVICE, 50051,"grpc"),
    "grpcs":      (FindingKind.CALLS_SERVICE, 443,  "grpc"),
    "tcp":        (FindingKind.CALLS_SERVICE, 0,    "tcp"),
    "ftp":        (FindingKind.CALLS_SERVICE, 21,   "ftp"),
    "sftp":       (FindingKind.CALLS_SERVICE, 22,   "ftp"),
    # Базы данных
    "mysql":      (FindingKind.CALLS_DB, 3306,  "db"),
    "mariadb":    (FindingKind.CALLS_DB, 3306,  "db"),
    "postgresql": (FindingKind.CALLS_DB, 5432,  "db"),
    "postgres":   (FindingKind.CALLS_DB, 5432,  "db"),
    "mongodb":    (FindingKind.CALLS_DB, 27017, "db"),
    "redis":      (FindingKind.CALLS_DB, 6379,  "db"),
    "rediss":     (FindingKind.CALLS_DB, 6380,  "db"),
    "cassandra":  (FindingKind.CALLS_DB, 9042,  "db"),
    "couchdb":    (FindingKind.CALLS_DB, 5984,  "db"),
    "elasticsearch": (FindingKind.CALLS_DB, 9200, "db"),
    "opensearch": (FindingKind.CALLS_DB, 9200,  "db"),
    "neo4j":      (FindingKind.CALLS_DB, 7687,  "db"),
    "bolt":       (FindingKind.CALLS_DB, 7687,  "db"),
    # Брокеры сообщений
    "amqp":       (FindingKind.ASYNC_CALL, 5672,  "mq"),
    "amqps":      (FindingKind.ASYNC_CALL, 5671,  "mq"),
    "kafka":      (FindingKind.ASYNC_CALL, 9092,  "mq"),
    "nats":       (FindingKind.ASYNC_CALL, 4222,  "mq"),
    "nats+tls":   (FindingKind.ASYNC_CALL, 4222,  "mq"),
    "stan":       (FindingKind.ASYNC_CALL, 4222,  "mq"),
    "mqtt":       (FindingKind.ASYNC_CALL, 1883,  "mq"),
    "mqtts":      (FindingKind.ASYNC_CALL, 8883,  "mq"),
    "zookeeper":  (FindingKind.ASYNC_CALL, 2181,  "mq"),
    # Почта
    "smtp":       (FindingKind.CALLS_SERVICE, 25,  "mail"),
    "smtps":      (FindingKind.CALLS_SERVICE, 465, "mail"),
    "imap":       (FindingKind.CALLS_SERVICE, 143, "mail"),
    "imaps":      (FindingKind.CALLS_SERVICE, 993, "mail"),
}

# JDBC sub-protocol → base protocol
_JDBC_MAP: dict[str, str] = {
    "mysql":      "mysql",
    "mariadb":    "mariadb",
    "postgresql": "postgresql",
    "oracle":     "postgresql",   # oracle не всегда известен, ставим DB
    "sqlserver":  "mysql",        # mssql
    "h2":         "mysql",        # embedded, всё равно тип db
    "sqlite":     "mysql",
    "db2":        "mysql",
}

# Паттерн для быстрого сканирования строк: находит вхождения протоколов
_PROTO_SCAN_RE = re.compile(
    r'(?:'
    # JDBC: jdbc:mysql://...
    r'jdbc:([a-z][a-z0-9+\-]*)://[^\s\'"<>]+'
    r'|'
    # Стандартные URL: protocol://...
    r'([a-z][a-z0-9+\-]*)://[^\s\'"<>]+'
    r')',
    re.IGNORECASE,
)


@dataclass
class ParsedURL:
    raw: str
    protocol: str           # "mysql", "http", "amqp", ...
    host: str
    port: int               # 0 если неизвестен
    path: str = ""
    username: str = ""
    db_name: str = ""
    kind: FindingKind = FindingKind.CONFIG_VALUE
    category: str = ""      # "http" | "db" | "mq" | "websocket" | ...
    default_port: bool = False  # True если порт взят из таблицы по умолчанию

    @property
    def is_variable(self) -> bool:
        """True если URL содержит переменную — не разворачиваем."""
        return "$" in self.raw or "${" in self.raw or "#{" in self.raw

    @property
    def is_bind_all(self) -> bool:
        """True если хост = 0.0.0.0 или :: (слушать на всех интерфейсах).

        Такой URL — это порт сервиса, а не удалённый хост.
        Потребитель должен нормализовать host в имя сервиса (имя директории).
        """
        return self.host in ("0.0.0.0", "::", "[::]")

    def service_name(self) -> str:
        """Имя хоста без домена — обычно совпадает с именем Docker-сервиса."""
        return self.host.split(".")[0] if self.host else ""


def parse_url(raw: str) -> ParsedURL | None:
    """Разобрать URL любого поддерживаемого протокола.

    Возвращает None если строка не похожа на URL или протокол неизвестен.
    """
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None

    # JDBC: jdbc:mysql://user:pass@host:3306/db
    jdbc_m = re.match(r'jdbc:([a-z][a-z0-9]*):(//)?(.+)', raw, re.IGNORECASE)
    if jdbc_m:
        sub = jdbc_m.group(1).lower()
        proto = _JDBC_MAP.get(sub, sub)
        remainder = jdbc_m.group(3)
        return _parse_remainder(raw, proto, remainder)

    # Стандартный URL: protocol://...
    std_m = re.match(r'([a-z][a-z0-9+\-]*)://(.+)', raw, re.IGNORECASE)
    if not std_m:
        return None

    proto = std_m.group(1).lower()
    remainder = std_m.group(2)

    if proto not in PROTOCOL_TABLE:
        return None

    return _parse_remainder(raw, proto, remainder)


def _parse_remainder(raw: str, proto: str, remainder: str) -> ParsedURL | None:
    """Разобрать часть URL после схемы."""
    kind, default_port, category = PROTOCOL_TABLE.get(
        proto, (FindingKind.CONFIG_VALUE, 0, "unknown")
    )

    # Убрать user:pass@
    auth_split = remainder.split("@", 1)
    if len(auth_split) == 2:
        username = auth_split[0].split(":")[0]
        remainder = auth_split[1]
    else:
        username = ""

    # Разделить host:port/path
    path_split = remainder.split("/", 1)
    host_port = path_split[0]
    path = "/" + path_split[1] if len(path_split) > 1 else ""
    db_name = path.lstrip("/").split("?")[0] if path else ""

    # host:port
    if ":" in host_port:
        host_raw, port_raw = host_port.rsplit(":", 1)
        port_raw = port_raw.split("?")[0]  # убрать query
        try:
            port = int(port_raw)
            default_port_flag = False
        except ValueError:
            port = default_port
            default_port_flag = True
            host_raw = host_port  # переменная вместо порта ($PORT)
    else:
        host_raw = host_port.split("?")[0]
        port = default_port
        default_port_flag = True

    # Убрать trailing слэши из host
    host = host_raw.strip("/")

    if not host:
        return None

    # 0.0.0.0 / :: — "слушать на всех интерфейсах", не реальный хост.
    # Сохраняем как есть: потребитель (DockerAdapter, PythonParser) нормализует
    # в имя сервиса. Здесь не удаляем — ParsedURL.is_bind_all сигнализирует.
    return ParsedURL(
        raw=raw,
        protocol=proto,
        host=host,
        port=port,
        path=path,
        username=username,
        db_name=db_name,
        kind=kind,
        category=category,
        default_port=default_port_flag,
    )


def scan_urls(text: str) -> list[ParsedURL]:
    """Найти все URL в произвольной строке."""
    results = []
    for m in _PROTO_SCAN_RE.finditer(text):
        matched = m.group(0)
        parsed = parse_url(matched)
        if parsed and not parsed.is_variable:
            results.append(parsed)
    return results
