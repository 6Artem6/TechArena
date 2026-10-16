import json
import sys

from pyvis.network import Network


def shorten_label(full_id: str, max_length: int = 25) -> str:
    """Сокращает длинные имена узлов для читаемости в визуализаторе"""
    if len(full_id) <= max_length:
        return full_id
    parts = full_id.split(".")
    if len(parts) > 2:
        # Оставляем первое слово пакета и имя класса/метода
        shortened = f"...{parts[-2]}.{parts[-1]}"
        return shortened
    return full_id[-max_length:]


def generate_graph_html(json_path: str, output_html: str = "graph_view.html"):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Инициализация сети (с поддержкой физики и зума)
    net = Network(height="750px", width="100%", directed=True, notebook=False)

    # Использование стандартного алгоритма физики без лишних аргументов
    net.force_atlas_2based(gravity=-50, central_gravity=0.01, spring_length=100)

    # Добавление узлов
    for node in data.get("nodes", []):
        node_id = node["id"]
        node_type = node.get("type", "default")
        annotations = node.get("annotations", [])

        # Сокращенное имя для отображения на графе
        label = shorten_label(node_id)

        # Цветовая дифференциация по типам
        if node_type == "class":
            color = "#ff7675"  # Коралловый для классов
            shape = "box"
        elif node_type == "method":
            color = "#74b9ff"  # Голубой для методов
            shape = "ellipse"
        else:
            color = "#dfe6e9"
            shape = "dot"

        # Формируем подсказку (tooltip) с полным именем и аннотациями
        title_text = f"<b>ID:</b> {node_id}<br><b>Type:</b> {node_type}"
        if annotations:
            title_text += "<br><b>Annotations:</b><br>" + "<br>".join(annotations)

        net.add_node(node_id, label=label, title=title_text, color=color, shape=shape)

    # Добавление ребер
    for edge in data.get("edges", []):
        source = edge["source"]
        target = edge["target"]
        edge_type = edge.get("type", "")
        net.add_edge(source, target, label=edge_type, arrows="to")

    # Включаем панель управления (фильтры/опции, если нужно)
    net.show_buttons(filter_=["physics"])

    # Сохранение в HTML
    net.save_graph(output_html)
    print(f"Визуализация успешно сохранена в файл: {output_html}")
    print("Откройте этот файл в любом браузере.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python visualize_graph.py <путь_к_json_графа>")
        sys.exit(1)

    generate_graph_html(sys.argv[1])
