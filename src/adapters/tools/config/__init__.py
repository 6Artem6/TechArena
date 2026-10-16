"""Адаптеры для конфигурационных файлов."""
from .nginx import NginxAdapter
from .yaml_adapter import YamlAdapter
from .docker import DockerAdapter
from .dotenv import DotenvAdapter
from .properties import PropertiesAdapter
from .deps import DepsAdapter

__all__ = [
    "NginxAdapter",
    "YamlAdapter",
    "DockerAdapter",
    "DotenvAdapter",
    "PropertiesAdapter",
    "DepsAdapter",
]
