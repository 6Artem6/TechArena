"""CLI точка входа: python -m src <command> [options]

Команды:
  analyze   — вычислить blast radius
  build     — построить граф и показать статистику
"""
import argparse
import sys
from pathlib import Path


def cmd_analyze(args: argparse.Namespace) -> None:
    from .graph.builder import GraphBuilder
    from .graph.bfs import ChangeSpec, compute_blast_radius
    from .output.serializer import print_result

    print(f"[INFO] Строим граф из {args.repo}...", file=sys.stderr)
    builder = GraphBuilder()
    graph = builder.build(Path(args.repo))
    print(
        f"[INFO] Граф: {builder.stats['nodes']} узлов, {builder.stats['edges']} рёбер "
        f"({builder.stats['files_parsed']} файлов, {builder.stats['files_error']} ошибок)",
        file=sys.stderr,
    )

    spec = ChangeSpec(
        service=args.service,
        file=args.file or "",
        class_fqn=args.cls or "",
        method=args.method or "",
    )
    result = compute_blast_radius(graph, spec, max_depth=args.max_depth)
    print_result(result, pretty=args.pretty)


def cmd_build(args: argparse.Namespace) -> None:
    import json
    from .graph.builder import GraphBuilder

    builder = GraphBuilder()
    graph = builder.build(Path(args.repo))
    stats = builder.stats

    # Топ CALLS_SERVICE рёбер
    calls = [
        (u, v) for u, v, d in graph.edges(data=True)
        if d.get("edge_type") == "CALLS_SERVICE"
    ]
    stats["calls_service_edges"] = len(calls)

    # Топ 10 хабов по out-degree по CALLS_SERVICE
    from collections import Counter
    out_deg = Counter(u for u, v in calls)
    stats["top_callers"] = out_deg.most_common(10)

    print(json.dumps(stats, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="src")
    sub = parser.add_subparsers(dest="command", required=True)

    # analyze
    p_analyze = sub.add_parser("analyze", help="Вычислить blast radius")
    p_analyze.add_argument("repo",           help="Путь к репозиторию (train-ticket/)")
    p_analyze.add_argument("--service",  "-s", required=True, help="Изменившийся сервис")
    p_analyze.add_argument("--file",     "-f", default="",    help="Путь к изменённому файлу")
    p_analyze.add_argument("--class",    "-c", dest="cls", default="", help="FQN класса")
    p_analyze.add_argument("--method",   "-m", default="",    help="Имя метода")
    p_analyze.add_argument("--max-depth",      default=6, type=int)
    p_analyze.add_argument("--pretty",         action="store_true", help="Красивый JSON")

    # build
    p_build = sub.add_parser("build", help="Построить граф и показать статистику")
    p_build.add_argument("repo", help="Путь к репозиторию")

    args = parser.parse_args()
    if args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "build":
        cmd_build(args)


if __name__ == "__main__":
    main()
