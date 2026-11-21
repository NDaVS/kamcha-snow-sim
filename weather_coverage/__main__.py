"""CLI for the OpenWeather coverage study."""
import argparse
import sys

from . import experiment, geography


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("требуется целое число больше нуля")
    return value


def main():
    parser = argparse.ArgumentParser(description="Пространственное покрытие OpenWeather")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("coordinates", help="пересобрать географическую выборку")
    commands.add_parser("demo", help="сгенерировать синтетические ответы")
    collect = commands.add_parser("collect", help="явно запустить запросы к бесплатному API")
    collect.add_argument("--round", required=True)
    collect.add_argument("--stage", choices=("pilot", "dense"), default="pilot")
    collect.add_argument("--grid", type=int, choices=geography.SIZES, default=500)
    collect.add_argument("--max-points", type=positive, default=20)
    collect.add_argument("--budget", type=positive)
    collect.add_argument("--recheck-access", action="store_true",
                         help="проверить доступ после HTTP 401/403 в новом срезе")
    collect.add_argument("--endpoints", nargs="+", choices=experiment.ENDPOINTS,
                         default=experiment.ENDPOINTS)
    show = commands.add_parser("map", help="пересобрать интерактивную карту")
    show.add_argument("--source", choices=("openweather", "demo"), default="openweather")
    args = parser.parse_args()
    try:
        if args.command == "coordinates":
            geography.build()
        elif args.command == "collect":
            experiment.run(args)
        elif args.command == "demo":
            if not (geography.HERE / "observations.csv").exists():
                experiment.write_csv(geography.HERE / "observations.csv", [])
            experiment.demo()
        elif args.command == "map":
            from .map_view import render
            render(args.source)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Остановка: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
