# Сверка погодных источников и модели снега

Из корня репозитория загрузите снимки общедоступных источников и сравните модельные ряды на общих датах и координатах.

```sh
python research/reality-validation-2026-11-21/fetch.py
python research/reality-validation-2026-11-21/recover_sources.py
python research/reality-validation-2026-11-21/weather_audit.py
```

Для повторной проверки расчёта снега запустите город по [инструкции](../../README_CITY_SIM.md) и установите зависимости из `requirements-city-sim.txt` в локальную среду Python.

```sh
python -m city_sim.reproduce_city_snow --event march-2025 --event december-2025 --event january-2026 --output out/citywide-weather
python -m research.reality-validation-2026-11-21.replay_audit out/citywide-weather
```

Загруженные материалы сохраняются в `sources/`, таблицы и расчётные результаты в `results/`.
