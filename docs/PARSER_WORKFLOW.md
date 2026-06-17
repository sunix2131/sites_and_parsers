# Parser fast workflow

Документ описывает новый быстрый рабочий режим парсера. Старый `run.py` не изменяется и продолжает работать как раньше.

## Запуск справки

```bash
python -m maps_parser.parser_workflow --help
```

## Профили скорости

Доступные профили:

| Профиль | Назначение |
| --- | --- |
| `safe` | Аккуратный режим: одна вкладка, больше пауз, меньше риск капчи |
| `normal` | Рабочий режим по умолчанию: баланс скорости и стабильности |
| `fast` | Быстрый режим для коротких тестов и небольших выборок |
| `long` | Долгий режим для больших городов и аккуратного добора лидов |

Профиль можно задать через `.env`:

```env
PARSER_PROFILE=normal
```

Или через аргумент команды:

```bash
--profile fast
```

## 1. Сбор только ссылок

Команда быстро собирает ссылки на карточки организаций `/org/`, но не открывает каждую карточку для глубокой проверки.

```bash
python -m maps_parser.parser_workflow collect-links --query "кафе" --location "Волгоград" --limit 200 --profile fast
```

Результат сохраняется в файл вида:

```text
out/org_urls_Волгоград_кафе_20260617_120000.csv
```

Этот режим удобен, когда нужно сначала быстро собрать базу карточек, а потом проверять её отдельным запуском.

## 2. Проверка готового файла ссылок

Команда принимает CSV или обычный текстовый файл со ссылками Яндекс.Карт вида `/org/`.

```bash
python -m maps_parser.parser_workflow check-links --input out/org_urls_Волгоград_кафе_20260617_120000.csv --only-no-site --contacts phone --profile normal
```

Полезные параметры:

| Параметр | Что делает |
| --- | --- |
| `--only-no-site` | сохраняет только организации с подтверждённым отсутствием сайта |
| `--contacts phone` | оставляет только лиды с телефоном |
| `--contacts any` | оставляет лиды с телефоном, email или соцссылкой |
| `--contacts all` | оставляет все найденные лиды |
| `--headful` | показывает окно Chromium |
| `--delay 3` | задаёт паузу между карточками вручную |

## 3. Быстрый запуск существующего парсера через профиль

```bash
python -m maps_parser.parser_workflow scrape-fast --query "кафе" --location "Волгоград" --limit 20 --prefer-no-site-stop --contacts phone --profile normal
```

Параметр `--prefer-no-site-stop` означает, что `--limit` воспринимается как цель по подходящим лидам, а не просто как количество открытых карточек.

## 4. Визуальный контроль браузера

```bash
python -m maps_parser.parser_workflow scrape-fast --query "кафе" --location "Волгоград" --limit 5 --headful --profile safe
```

## Рекомендуемый рабочий сценарий

1. Сначала собрать ссылки:

```bash
python -m maps_parser.parser_workflow collect-links --query "кафе" --location "Волгоград" --limit 300 --profile fast
```

2. Потом проверить файл:

```bash
python -m maps_parser.parser_workflow check-links --input out/org_urls_Волгоград_кафе_20260617_120000.csv --only-no-site --contacts phone --profile normal
```

3. Если Яндекс начинает показывать служебные страницы, перейти на профиль `safe` или `long`.

## Что изменилось технически

- Добавлен отдельный entrypoint `maps_parser.parser_workflow`.
- Добавлен модуль `maps_parser/link_tools.py`.
- Добавлены профили скорости в `maps_parser/run_modes.py`.
- Добавлен параметр `PARSER_PROFILE` в настройки и `.env.example`.
- Старый `run.py` не сломан и остаётся совместимым.
