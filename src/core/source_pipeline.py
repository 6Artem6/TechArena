"""Фабрика ToolRegistry с дефолтным набором адаптеров.

Изолирует конфигурацию pipeline от GraphBuilder: builder принимает готовый
registry через DI, а source_pipeline.py знает какие адаптеры существуют и в
каком порядке их регистрировать.

Добавить новый адаптер: импортировать и добавить в список adapter_classes.
"""
from __future__ import annotations

from ..adapters.tools.registry import ToolRegistry


def default_registry() -> ToolRegistry:
    """Создать ToolRegistry со всеми доступными адаптерами.

    Адаптеры регистрируются в порядке приоритета (высший — первый).
    is_available() каждого адаптера проверяется при регистрации —
    недоступные инструменты пропускаются молча.
    """
    from ..adapters.tools.config.yaml_adapter import YamlAdapter
    from ..adapters.tools.config.nginx import NginxAdapter
    from ..adapters.tools.config.properties import PropertiesAdapter
    from ..adapters.tools.config.docker import DockerAdapter
    from ..adapters.tools.config.dotenv import DotenvAdapter
    from ..adapters.tools.config.deps import DepsAdapter
    from ..adapters.tools.code.test_adapter import TestAdapter

    adapter_classes = [
        YamlAdapter,       # application.yml, docker-compose, k8s
        NginxAdapter,      # nginx.conf → PROXY_ROUTE
        PropertiesAdapter, # application.properties → CALLS_DB, ASYNC_CALL
        DockerAdapter,     # Dockerfile → SERVICE_DEF
        DotenvAdapter,     # .env → ENV_ACCESS + URLs
        DepsAdapter,       # pom.xml / package.json / go.mod → IMPORT
        TestAdapter,       # тест-файлы → CALLS_SERVICE (mock-URL, conf=0.85)
    ]

    registry = ToolRegistry()
    for cls in adapter_classes:
        registry.register(cls())
    return registry
