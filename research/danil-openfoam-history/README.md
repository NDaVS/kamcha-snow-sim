# Прямой расчёт обтекания в OpenFOAM для трёх исторических часов

Установите зависимости из `requirements.txt` и OpenFOAM v2512. Если OpenFOAM установлен вне стандартного каталога, задайте `OPENFOAM_BASHRC` по [инструкции решателя](../../README.md#расчёт-потока-для-выбранного-тайла).

Из корня репозитория запустите API Данила.

```sh
python -m uvicorn snowmap.api:app --host 127.0.0.1 --port 8011
```

Во втором терминале из корня репозитория запустите сравнение прямого расчёта с шаблонным.

```sh
python research/danil-openfoam-history/replay.py
```

В `inputs.json` сохранены три часа реанализа ECMWF IFS, координаты точки и ссылки на Open-Meteo. Скрипт записывает растры и сводку в `out/danil-openfoam-history`. [Источник данных](https://open-meteo.com/en/docs/historical-weather-api) · [Лицензия CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
