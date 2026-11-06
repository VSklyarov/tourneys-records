# Pool · volume & duration (static)

Статический отчёт: `pool-tourney-volume-record.html` + шрифты в `fonts/`. Деплой на Vercel — **статический сайт**, без сборки.

## Локально

Открой `pool-tourney-volume-record.html` в браузере (двойной клик) или подними любой static server в этой папке.

## Деплой на Vercel через GitHub

1. Создай новый репозиторий на GitHub (например `tourneys-records`) — **без** README/license, чтобы было проще первый push.
2. В этой папке выполни (подставь свой URL):

   ```bash
   git init -b main
   git add pool-tourney-volume-record.html fonts/ vercel.json README.md .gitignore
   git add build_tourney_volume_report.py _cache_limits.json pool-tourney-volume-record-chart-data.js
   git commit -m "Add pool volume report static site"
   git remote add origin https://github.com/VSklyarov/ИМЯ-РЕПО.git
   git push -u origin main
   ```

   При желании не коммить генератор и кэши — достаточно первой строки `git add` для работы сайта.

3. На [vercel.com](https://vercel.com) → **Add New → Project** → **Import** этот репозиторий.
4. Настройки: **Framework Preset: Other**, **Root Directory: `.`** (корень репо), **Build Command: пусто**, **Output: пусто / default**.
5. Deploy. Главная `/` перенаправляется на отчёт через `vercel.json`.

Прямая ссылка на страницу: `https://<твой-проект>.vercel.app/pool-tourney-volume-record.html`.

## Перегенерация HTML

Нужны Python-зависимости и доступ к БД (или `REFRESH_DATA` не трогать и использовать `_cache_limits.json`). См. `build_tourney_volume_report.py`.
