"""Java-парсер на базе tree-sitter.

Извлекает структуру файла (пакет, классы, методы, аннотации) и
межсервисные связи (getServiceUrl, @FeignClient, HTTP URL-литералы).

Стратегия:
  - tree-sitter: структурные элементы (package, class, method, modifiers)
  - regex: вызовы сервисов (getServiceUrl, HTTP-литералы, @FeignClient)

Почему regex для вызовов: tree-sitter даёт точные позиции, но traversal
глубокого дерева для method_invocation в 10К файлов медленнее, чем
компилированный regex по байтовому буферу.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

from .base import EndpointInfo, LanguageParser, ParsedFile

# ─── Инициализация парсера ────────────────────────────────────────────────────

_JAVA_LANG = Language(tsjava.language())
_TS_PARSER = Parser(_JAVA_LANG)

# ─── Regex для межсервисных вызовов ──────────────────────────────────────────

# getServiceUrl("ts-xxx-service")  — основной паттерн train-ticket (279 вызовов)
_GET_SERVICE_URL = re.compile(r'getServiceUrl\s*\(\s*"(ts-[^"]+)"')

# @FeignClient(name="ts-xxx") или @FeignClient("ts-xxx")
_FEIGN_CLIENT = re.compile(
    r'@FeignClient\s*\(\s*(?:name\s*=\s*)?["\']?(ts-[^"\')\s,]+)'
)

# Прямые HTTP-литералы: "http://ts-xxx:PORT" или "${HOST:ts-xxx}"
_HTTP_LITERAL = re.compile(
    r'"(?:http://|https://)(ts-[a-z0-9\-]+)(?::\d+)?'
    r'|(?:\$\{[^:}]+:)(ts-[a-z0-9\-]+)(?::\d+)?'
)

# import org.example.Foo; или import static org.example.Foo.*;
_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?;",
    re.MULTILINE,
)

# RabbitMQ: rabbitTemplate.convertAndSend("queue", val) или (Queues.queueName, val)
_RABBIT_SEND = re.compile(
    r'(?:rabbitTemplate|amqpTemplate|this\.rabbitTemplate|this\.amqpTemplate)'
    r'\s*\.\s*convertAndSend\s*\(\s*([^,)]+)',
)

# @RabbitListener(queues = "queue") или (queues = Queues.queueName)
_RABBIT_LISTEN = re.compile(
    r'@RabbitListener\s*\(\s*queues\s*=\s*\{?\s*([^,})\s]+)',
)

# @deprecated / * @deprecated / // Deprecated / // deprecated
_DEPRECATED_RE = re.compile(
    r'@[Dd]eprecated|/\*.*?@deprecated.*?\*/|//\s*[Dd]eprecated',
    re.DOTALL,
)

# FIXME / BUG / HACK / XXX в комментариях
_BUG_NOTE_RE = re.compile(
    r'//\s*(?:FIXME|BUG|HACK|XXX)[:\s](.{0,80})|'
    r'/\*\s*(?:FIXME|BUG|HACK|XXX)[:\s](.{0,80})',
)

# //N. step description — бизнес-шаги метода (ADR-020 §E.1)
_FLOW_STEP_RE = re.compile(r'//\s*(\d+)\.\s*(.+)', re.MULTILINE)

# LOGGER.[info|warn|error]("[tag1][tag2][tag3]", ...)
_LOGGER_TAG_RE = re.compile(
    r'LOGGER\s*\.\s*(?:info|warn|error|debug)\s*\(\s*"(\[[^\]]+\](?:\[[^\]]+\])*)',
)
_LOGGER_BRACKET_RE = re.compile(r'\[([^\[\]]+)\]')

# public static final String FIELD = "value";
_STATIC_CONST = re.compile(
    r'(?:public\s+|protected\s+)?(?:static\s+final|final\s+static)\s+String\s+'
    r'(\w+)\s*=\s*"([^"]+)"\s*;',
    re.MULTILINE,
)

# @RequestMapping / @GetMapping / @PostMapping / @PutMapping / @DeleteMapping / @PatchMapping
_MAPPING_ANN = re.compile(
    r'@((?:Request|Get|Post|Put|Delete|Patch)Mapping)'
    r'\s*\((?:[^)]*(?:value|path)\s*=\s*)?["\']([^"\']+)["\']'
)

# ─── Java-специфичные типы ────────────────────────────────────────────────────

@dataclass
class JavaEndpointInfo(EndpointInfo):
    """Endpoint с Java-специфичными деталями (class_fqn, method_name)."""
    class_fqn: str = ""
    method_name: str = ""


@dataclass
class MethodInfo:
    name: str
    annotations: list[str] = field(default_factory=list)
    endpoints: list[JavaEndpointInfo] = field(default_factory=list)
    calls_services: list[str] = field(default_factory=list)
    # ADR-020 §E: метаданные из комментариев
    flow_steps: list[tuple[int, str]] = field(default_factory=list)  # [(1, "detect scalper"), ...]
    logger_tags: list[str] = field(default_factory=list)             # ["preserve", "Step 1", "action"]


@dataclass
class ClassInfo:
    fqn: str          # package.ClassName
    kind: str         # class | interface | enum
    annotations: list[str] = field(default_factory=list)
    methods: list[MethodInfo] = field(default_factory=list)
    base_path: str = ""
    static_constants: dict[str, str] = field(default_factory=dict)  # FIELD → "value"
    is_abstract: bool = False
    extends_class: str = ""                      # суперкласс (для REPOSITORY-детекции)
    implements_interfaces: list[str] = field(default_factory=list)


@dataclass
class ParsedJavaFile:
    """Результат парсинга Java-файла.

    Реализует тот же протокол что и ParsedFile — structural subtyping.
    `package` хранит Java-пакет; в GraphBuilder используется как `module`.
    """
    service: str
    path: str
    language: str = "java"
    package: str = ""        # Java package → используется как module в PackageIndex
    classes: list[ClassInfo] = field(default_factory=list)
    calls_services: list[str] = field(default_factory=list)
    endpoints: list[JavaEndpointInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    listen_port: int = 0
    error: str = ""
    # RabbitMQ: сырые выражения из кода (до resolve через SymbolIndex)
    sends_queue_exprs: list[str] = field(default_factory=list)
    receives_queue_exprs: list[str] = field(default_factory=list)
    # Метаданные уровня файла (ADR-020)
    deprecated: bool = False
    has_bug_note: bool = False
    bug_description: str = ""

    @property
    def module(self) -> str:
        return self.package


# ─── Парсер ───────────────────────────────────────────────────────────────────

class JavaParser:
    """Парсер Java-файлов: tree-sitter структура + regex вызовы.

    Реализует LanguageParser Protocol.
    """

    supported_language: str = "java"

    def parse(self, path: Path, service: str) -> ParsedJavaFile:
        try:
            src = path.read_bytes()
        except OSError as e:
            return ParsedJavaFile(service=service, path=str(path), package="", error=str(e))

        result = ParsedJavaFile(service=service, path=str(path), package="")
        try:
            tree = _TS_PARSER.parse(src)
            self._extract_structure(tree.root_node, src, result)
        except Exception as e:
            result.error = f"tree-sitter: {e}"
            # Fallback: regex-only для самого важного
            self._regex_fallback(src.decode("utf-8", errors="replace"), result)
            return result

        # Regex по всему тексту файла — для вызовов (быстрее чем обход дерева)
        text = src.decode("utf-8", errors="replace")
        self._extract_calls(text, result)
        return result

    # ─── tree-sitter структурный обход ────────────────────────────────────────

    def _extract_structure(self, root, src: bytes, result: ParsedJavaFile) -> None:
        text = src.decode("utf-8", errors="replace")
        current_class: ClassInfo | None = None

        def node_text(n) -> str:
            return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

        def traverse(node, class_ctx: ClassInfo | None = None):
            nonlocal current_class

            if node.type == "package_declaration":
                for child in node.children:
                    if child.type in ("identifier", "scoped_identifier"):
                        result.package = node_text(child)

            elif node.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                kind = "interface" if "interface" in node.type else (
                    "enum" if "enum" in node.type else "class"
                )
                name = ""
                annotations: list[str] = []
                is_abstract = False
                extends_class = ""
                implements_ifaces: list[str] = []

                for child in node.children:
                    if child.type == "identifier":
                        name = node_text(child)
                    elif child.type == "modifiers":
                        for mod in child.children:
                            if "annotation" in mod.type:
                                annotations.append(node_text(mod))
                            elif mod.type == "abstract":
                                is_abstract = True
                    elif child.type == "superclass":
                        # superclass: "extends ClassName"
                        for sc in child.children:
                            if sc.type in ("type_identifier", "generic_type"):
                                extends_class = node_text(sc).split("<")[0].strip()
                    elif child.type in ("super_interfaces", "extends_interfaces"):
                        # super_interfaces: "implements A, B" (class)
                        # extends_interfaces: "extends A, B" (interface)
                        for iface in child.children:
                            if iface.type == "type_list":
                                for t in iface.children:
                                    if t.type in ("type_identifier", "generic_type"):
                                        implements_ifaces.append(
                                            node_text(t).split("<")[0].strip()
                                        )

                if name:
                    fqn = f"{result.package}.{name}" if result.package else name
                    cls = ClassInfo(
                        fqn=fqn,
                        kind=kind,
                        annotations=annotations,
                        is_abstract=is_abstract,
                        extends_class=extends_class,
                        implements_interfaces=implements_ifaces,
                    )
                    # Базовый путь из @RequestMapping на классе
                    cls.base_path = _extract_base_path(annotations)
                    result.classes.append(cls)
                    current_class = cls
                    for child in node.children:
                        traverse(child, cls)
                    return  # не повторять обход детей ниже

            elif node.type == "method_declaration" and class_ctx is not None:
                method_name = ""
                annotations: list[str] = []
                for child in node.children:
                    if child.type == "identifier" and not method_name:
                        method_name = node_text(child)
                    elif child.type == "modifiers":
                        for mod in child.children:
                            if "annotation" in mod.type:
                                annotations.append(node_text(mod))

                if method_name:
                    mi = MethodInfo(name=method_name, annotations=annotations)
                    # Endpoints из аннотаций метода
                    for ep in _parse_endpoint_annotations(annotations, class_ctx.base_path,
                                                          class_ctx.fqn, method_name):
                        mi.endpoints.append(ep)
                        result.endpoints.append(ep)
                    class_ctx.methods.append(mi)

            for child in node.children:
                traverse(child, class_ctx)

        traverse(root)

    # ─── Regex: межсервисные вызовы и импорты ────────────────────────────────

    def _extract_calls(self, text: str, result: ParsedJavaFile) -> None:
        called: set[str] = set()

        for m in _GET_SERVICE_URL.finditer(text):
            called.add(m.group(1))

        for m in _FEIGN_CLIENT.finditer(text):
            called.add(m.group(1))

        for m in _HTTP_LITERAL.finditer(text):
            svc = m.group(1) or m.group(2)
            if svc:
                called.add(svc)

        result.calls_services = sorted(called)

        # Импорты — для PackageIndex → USES_DTO рёбер
        result.imports = [m.group(1) for m in _IMPORT_RE.finditer(text)]

        # RabbitMQ: сырые выражения очередей (будут resolve в SymbolIndex)
        sends: set[str] = set()
        for m in _RABBIT_SEND.finditer(text):
            expr = m.group(1).strip().strip('"').strip("'")
            if expr:
                sends.add(expr)
        result.sends_queue_exprs = sorted(sends)

        receives: set[str] = set()
        for m in _RABBIT_LISTEN.finditer(text):
            expr = m.group(1).strip().strip('"').strip("'")
            if expr:
                receives.add(expr)
        result.receives_queue_exprs = sorted(receives)

        # Статические константы из тела класса → ClassInfo.static_constants
        for cls in result.classes:
            for m in _STATIC_CONST.finditer(text):
                cls.static_constants[m.group(1)] = m.group(2)

        # Метаданные файла: deprecated / has_bug_note (ADR-020 §E)
        if _DEPRECATED_RE.search(text):
            result.deprecated = True
        bug_m = _BUG_NOTE_RE.search(text)
        if bug_m:
            result.has_bug_note = True
            result.bug_description = (bug_m.group(1) or bug_m.group(2) or "").strip()

        # flow_steps и logger_tags — привязываем к методу через tree-sitter
        # (делаем упрощённо: ищем в тексте ближайшем к каждому методу)
        self._extract_method_metadata(text, result)

    def _extract_method_metadata(self, text: str, result: ParsedJavaFile) -> None:
        """Привязать flow_steps и logger_tags к методам по позиции в тексте.

        Стратегия: для каждого метода берём блок 2000 символов от его позиции
        (приближение — tree-sitter даёт точные позиции, но здесь хватит regex).
        """
        # Находим позиции методов через простой regex
        method_pos_re = re.compile(
            r'(?:public|protected|private|static|\s)+\s+\w[\w<>\[\]]*\s+(\w+)\s*\(',
            re.MULTILINE,
        )
        # Индекс имя→MethodInfo для быстрого поиска
        method_by_name: dict[str, "MethodInfo"] = {}
        for cls in result.classes:
            for mi in cls.methods:
                method_by_name[mi.name] = mi

        if not method_by_name:
            return

        # Ищем позиции методов и извлекаем блок после каждого
        method_positions: list[tuple[int, str]] = []
        for m in method_pos_re.finditer(text):
            name = m.group(1)
            if name in method_by_name:
                method_positions.append((m.start(), name))

        for i, (pos, name) in enumerate(method_positions):
            end = method_positions[i + 1][0] if i + 1 < len(method_positions) else pos + 3000
            block = text[pos:min(end, pos + 3000)]
            mi = method_by_name[name]

            # flow_steps: //N. description
            for fm in _FLOW_STEP_RE.finditer(block):
                mi.flow_steps.append((int(fm.group(1)), fm.group(2).strip()))

            # logger_tags
            for lm in _LOGGER_TAG_RE.finditer(block):
                tags = _LOGGER_BRACKET_RE.findall(lm.group(1))
                mi.logger_tags.extend(tags)

    def _regex_fallback(self, text: str, result: ParsedJavaFile) -> None:
        """Минимальный парсинг когда tree-sitter упал."""
        m = re.search(r'^package\s+([\w.]+)\s*;', text, re.MULTILINE)
        if m:
            result.package = m.group(1)
        self._extract_calls(text, result)


# ─── Вспомогательные функции ─────────────────────────────────────────────────

_HTTP_METHOD_MAP = {
    "GetMapping":    "GET",
    "PostMapping":   "POST",
    "PutMapping":    "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping":  "PATCH",
    "RequestMapping": "*",
}


def _extract_base_path(annotations: list[str]) -> str:
    for ann in annotations:
        m = _MAPPING_ANN.search(ann)
        if m:
            return m.group(2)
    return ""


def _parse_endpoint_annotations(
    annotations: list[str],
    base_path: str,
    class_fqn: str,
    method_name: str,
) -> list[EndpointInfo]:
    results = []
    for ann in annotations:
        m = _MAPPING_ANN.search(ann)
        if not m:
            # Попробуем форму без value= : @GetMapping("/path")
            m2 = re.search(
                r'@((?:Request|Get|Post|Put|Delete|Patch)Mapping)\s*\(\s*["\']([^"\']+)["\']',
                ann,
            )
            if m2:
                ann_name, path = m2.group(1), m2.group(2)
            else:
                continue
        else:
            ann_name, path = m.group(1), m.group(2)

        http_method = _HTTP_METHOD_MAP.get(ann_name, "*")
        full_path = (base_path.rstrip("/") + "/" + path.lstrip("/")).rstrip("/") or "/"
        results.append(JavaEndpointInfo(
            path=full_path,
            method=http_method,
            class_fqn=class_fqn,
            method_name=method_name,
        ))
    return results
