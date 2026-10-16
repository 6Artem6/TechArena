"""NodeId — формат идентификаторов узлов графа.

Формат: <service>::<fqn>
Примеры:
  ts-travel-service::travel.service.TravelServiceImpl
  ts-travel-service::travel.service.TravelServiceImpl.create
  ts-travel-service::  (SERVICE-узел самого сервиса)

Правило нормализации применяется при сравнении с gold standard.
Текущий формат — заглушка до получения gold standard контракта.
Замена формата = одна функция make_*_id + normalize_node_id.
"""
from __future__ import annotations

# Разделитель компонентов внутри FQN (пакет.Класс.метод)
_SEP = "::"


class NodeId(str):
    """Строка-обёртка для узловых идентификаторов.

    Наследует str, чтобы работало в NetworkX как обычная строка.
    """
    __slots__ = ()


def make_service_id(service: str) -> NodeId:
    return NodeId(service)


def make_class_id(service: str, fqn: str) -> NodeId:
    """fqn = package.ClassName"""
    return NodeId(f"{service}{_SEP}{fqn}")


def make_method_id(service: str, fqn: str, method: str) -> NodeId:
    """fqn = package.ClassName"""
    return NodeId(f"{service}{_SEP}{fqn}.{method}")


def normalize_node_id(raw: str) -> NodeId:
    """Привести произвольную строку к каноническому NodeId.

    Заглушка — при получении gold standard здесь будет реальная нормализация.
    Пока: убрать пробелы, заменить '/' → '.'.
    """
    return NodeId(raw.strip().replace("/", "."))
