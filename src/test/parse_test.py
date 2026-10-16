import sys

import tree_sitter_go as tsgo
import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjs
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

LANGUAGES = {
    "java": Language(tsjava.language()),
    "py": Language(tspython.language()),
    "js": Language(tsjs.language()),
    "go": Language(tsgo.language()),
}


def print_tree(node, depth=0, max_depth=3):
    """Рекурсивный вывод структуры дерева с ограничением по глубине"""
    if depth > max_depth:
        return
    indent = "  " * depth
    print(f"{indent}{node.type} [{node.start_point} - {node.end_point}]")
    for child in node.children:
        print_tree(child, depth + 1, max_depth)


def parse_file(file_path: str, lang_key: str):
    if lang_key not in LANGUAGES:
        print(f"Ошибка: язык {lang_key} не поддерживается.")
        return

    parser = Parser(LANGUAGES[lang_key])

    with open(file_path, "rb") as f:
        code_bytes = f.read()

    tree = parser.parse(code_bytes)
    root_node = tree.root_node

    print(f"=== Результат парсинга: {file_path} ({lang_key}) ===")
    print(f"Корневой узел: {root_node.type}")
    print(f"Всего дочерних элементов: {len(root_node.children)}\n")

    print("Структура дерева (первые 3 уровня):")
    print_tree(root_node, depth=0, max_depth=3)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Использование: python parse_test.py <путь_к_файлу> <язык: java/py/js/go>")
        sys.exit(1)

    parse_file(sys.argv[1], sys.argv[2])
