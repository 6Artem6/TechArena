import json
import sys

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

JAVA_LANG = Language(tsjava.language())


def extract_node_text(node, source_bytes):
    return source_bytes[node.start_byte : node.end_byte].decode("utf8")


def parse_java_to_graph(file_path: str):
    parser = Parser(JAVA_LANG)

    with open(file_path, "rb") as f:
        code_bytes = f.read()

    tree = parser.parse(code_bytes)
    root_node = tree.root_node

    graph_nodes = []
    graph_edges = []

    current_package = ""
    current_class = None

    # Простой обход в глубину для поиска пакетов, классов, аннотаций и методов
    def traverse(node):
        nonlocal current_package, current_class

        if node.type == "package_declaration":
            for child in node.children:
                if child.type == "identifier" or child.type == "scoped_identifier":
                    current_package = extract_node_text(child, code_bytes)

        elif node.type == "class_declaration":
            class_name = ""
            annotations = []

            # Ищем имя класса и аннотации
            for child in node.children:
                if child.type == "identifier":
                    class_name = extract_node_text(child, code_bytes)
                elif child.type == "modifiers":
                    for mod in child.children:
                        if "annotation" in mod.type:
                            annotations.append(extract_node_text(mod, code_bytes))

            current_class = f"{current_package}.{class_name}" if current_package else class_name

            graph_nodes.append(
                {
                    "id": current_class,
                    "type": "class",
                    "annotations": annotations,
                    "file": file_path,
                }
            )

        elif node.type == "method_declaration":
            method_name = ""
            for child in node.children:
                if child.type == "identifier":
                    method_name = extract_node_text(child, code_bytes)

            if current_class and method_name:
                method_id = f"{current_class}.{method_name}"
                graph_nodes.append({"id": method_id, "type": "method", "file": file_path})
                # Связываем класс и метод ребром
                graph_edges.append(
                    {"source": current_class, "target": method_id, "type": "CONTAINS"}
                )

        for child in node.children:
            traverse(child)

    traverse(root_node)

    return {"nodes": graph_nodes, "edges": graph_edges}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python graph_extractor.py <путь_к_java_файлу>")
        sys.exit(1)

    result = parse_java_to_graph(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False))
