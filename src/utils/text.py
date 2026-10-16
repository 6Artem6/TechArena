"""Утилиты для синтаксически-корректной обрезки текста и кода.

Простое правило: никогда не рвать посередине синтаксической единицы.
Три варианта в порядке убывания «уважения» к структуре:
  truncate_code()  — граница = последний перевод строки (\n)
  truncate_prose() — граница = конец предложения (.!?)
  smart_truncate() — диспетчер: выбирает режим по содержимому
"""
import re

# Символы конца предложения
_SENTENCE_END = re.compile(r'[.!?](?:\s|$)')


def truncate_code(text: str, max_chars: int) -> str:
    """Обрезать код по последней целой строке перед max_chars.

    Гарантирует, что результат не заканчивается на половине строки.
    Если одна строка длиннее max_chars — возвращает её целиком (не рвём
    идентификаторы).
    """
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    if cut == -1:
        # Нет переноса строки вовсе — обрезаем по последнему пробелу (токен-граница)
        space = text.rfind(" ", 0, max_chars)
        if space > 0:
            return text[:space]
        # Нет ни \n ни пробела — одно длинное слово, берём до конца текущей строки
        cut = text.find("\n", max_chars)
        return text if cut == -1 else text[:cut]
    return text[:cut]


def truncate_prose(text: str, max_chars: int) -> str:
    """Обрезать прозу (комментарии, docstring) по концу предложения.

    Ищет последнее вхождение [.!?] перед max_chars и обрезает после него.
    Если предложений нет — падает до truncate_code (граница строки).
    """
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    # Ищем последний конец предложения в окне
    last = -1
    for m in _SENTENCE_END.finditer(window):
        last = m.start() + 1  # включаем сам знак препинания
    if last > 0:
        return text[:last].rstrip()
    # Нет предложений — откатываемся до границы строки
    return truncate_code(text, max_chars)


def smart_truncate(text: str, max_chars: int, *, is_code: bool | None = None) -> str:
    """Синтаксически-корректная обрезка: диспетчер.

    Определяет режим автоматически, если is_code не задан:
      - текст с высокой долей \n и { } → код
      - иначе → проза

    Args:
        text:      входной текст
        max_chars: мягкий лимит в символах (может быть превышен на одну строку/предложение)
        is_code:   принудительный режим; None = автодетект
    """
    if len(text) <= max_chars:
        return text

    if is_code is None:
        newline_density = text.count("\n") / max(len(text), 1)
        brace_density = (text.count("{") + text.count("}")) / max(len(text), 1)
        is_code = (newline_density > 0.03) or (brace_density > 0.01)

    return truncate_code(text, max_chars) if is_code else truncate_prose(text, max_chars)
