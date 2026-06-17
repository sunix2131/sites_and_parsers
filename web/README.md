# LeadCRM - Система управления лидами

Веб-сервис для управления лидами, собранными парсером Яндекс Карт.

## Стек

- **Backend**: FastAPI + SQLAlchemy + PostgreSQL
- **Frontend**: React + Vite + TypeScript + Tailwind CSS

## Быстрый старт

### 1. База данных

```bash
sudo -u postgres createdb leadcrm
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'password';"
```

### 2. Backend

```bash
cd web/backend
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env
# Отредактируйте .env (DATABASE_URL, SECRET_KEY)
python run.py
```

Backend запустится на `http://localhost:8000`

### 3. Импорт лидов из CSV

```bash
cd web/backend
python import_csv.py ../../out/no_site_leads.csv
```

### 4. Frontend

```bash
cd web/frontend
npm install
npm run dev
```

Frontend запустится на `http://localhost:5173`

## Доступ

- **Админ**: логин `admin`, пароль `admin123` (измените в .env!)
- **Продавцы**: создаются администратором через панель управления

## Функции

### Администратор
- Просмотр статистики по лидам и продавцам
- Создание/управление аккаунтами продавцов
- Назначение лидов продавцам (по одному или пакетно)
- Просмотр всех лидов с фильтрацией и поиском

### Продавец
- Список назначенных лидов с контактами
- Кнопка "Позвонить" с таймером разговора
- Обновление статуса: Согласен / Отказ / Перезвонить / Не ответил
- Заметки к каждому лиду
- Ограничение: 25 обновлений статуса в час (защита от случайных нажатий)

## Rate Limiting

Каждый продавец может обновить статус лида не более 25 раз в час.
Это предотвращает случайные нажатия, но позволяет обработать 20-30 лидов в час.
