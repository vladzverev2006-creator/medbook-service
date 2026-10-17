# Техническая спецификация проекта «MedBook» (Онлайн-запись к врачам)

## 1. Стек технологий
- **Backend:** Python 3.12+, FastAPI, SQLAlchemy 2.0 (async или sync), Pydantic v2, Alembic (миграции).
- **База данных:** PostgreSQL 16 (реализация ER-диаграммы, транзакции CP).
- **Кэш и распределённые блокировки:** Redis 7 (Cache-aside для расписания + Distributed Lock `SET NX EX 600` для защиты от двойного бронирования).
- **Имитация внешних сервисов (Mock):** Отдельный легковесный FastAPI-сервис `mock_gateway`, имитирующий платёжный шлюз (`/mock/pay`) и SMS/Email-шлюз (`/mock/notify`).
- **Контейнеризация:** Docker + `docker-compose.yml` (сервисы: `backend`, `postgres`, `redis`, `mock-gateway`).
- **Тестирование:** `pytest` + `httpx` (интеграционные тесты ключевых сценариев и гонки бронирования).

## 2. Схема базы данных (ER-диаграмма — 6 таблиц)
Реализовать в `app/models.py` и через миграции Alembic:
1. `users`:
   - `id` (PK, int)
   - `email` (String, unique, index, not null)
   - `phone` (String, not null)
   - `full_name` (String, not null)
   - `role` (String: `patient`, `doctor`, `admin`, default=`patient`)
   - `hashed_password` (String, not null)
   - `created_at` (DateTime)
2. `clinics`:
   - `id` (PK, int)
   - `name` (String, not null)
   - `city` (String, index, not null)
   - `address` (String, not null)
   - `rating` (Float, default=5.0)
3. `specializations`:
   - `id` (PK, int)
   - `name` (String, unique, not null)
   - `description` (String)
4. `doctors`:
   - `id` (PK, int)
   - `clinic_id` (FK -> `clinics.id`, not null)
   - `specialization_id` (FK -> `specializations.id`, not null)
   - `full_name` (String, not null)
   - `experience_years` (Integer, default=1)
   - `price` (Numeric(10, 2), not null)
   - `rating` (Float, default=5.0)
   - *Индекс:* составной индекс `idx_doctors_clinic_spec` по `(clinic_id, specialization_id)`.
5. `time_slots`:
   - `id` (PK, int)
   - `doctor_id` (FK -> `doctors.id`, not null)
   - `start_time` (DateTime, not null)
   - `end_time` (DateTime, not null)
   - `is_available` (Boolean, default=True, not null)
   - *Индекс:* составной индекс `idx_timeslots_doctor_start` по `(doctor_id, start_time, is_available)`.
6. `appointments` (Центральная сущность):
   - `id` (PK, int)
   - `user_id` (FK -> `users.id`, not null)
   - `doctor_id` (FK -> `doctors.id`, not null)
   - `slot_id` (FK -> `time_slots.id`, not null)
   - `status` (String: `PENDING`, `CONFIRMED`, `CANCELLED`, `COMPLETED`, default=`PENDING`)
   - `payment_status` (String: `UNPAID`, `PAID`, `REFUNDED`, default=`UNPAID`)
   - `notes` (String, nullable)
   - `created_at` (DateTime)
   - *Индекс:* составной индекс `idx_appointments_user` по `(user_id, created_at)`.

## 3. Обязательные методы REST API и сервисные эндпоинты
1. `POST /api/v1/auth/register` — регистрация пользователя (роль `patient`).
2. `POST /api/v1/auth/login` — вход и выдача JWT токена (`Bearer`).
3. `GET /api/v1/clinics` (фильтр `?city=`) и `POST /api/v1/clinics` (только `admin`) — список и создание клиник.
4. `GET /api/v1/specializations` и `POST /api/v1/specializations` (только `admin`) — справочник специализаций.
5. `GET /api/v1/doctors` (фильтры `?clinic_id=`, `?specialization_id=`, `?city=`) — поиск врачей с кэшированием в Redis (паттерн Cache-Aside, TTL 60 сек) и `POST /api/v1/doctors` (только `admin`) — создание врача с проверкой `clinic_id` и `specialization_id` и инвалидацией кэша `cache:doctors:*`.
6. `GET /api/v1/doctors/{id}/slots` — получение слотов врача (предварительно вызывает `cleanup_expired_pending_appointments`, кэширование в Redis на 30 сек).
7. `POST /api/v1/doctors/{id}/slots` (только `admin`) — добавление слота в расписание врача (с проверкой пересечений) и инвалидация кэша слотов этого врача (`cache:slots:{id}:*`).
8. `POST /api/v1/appointments` — **ключевой метод (сложный CP-узел)**:
   - Принимает `slot_id` и `notes` (требует JWT авторизации).
   - Выполняет `cleanup_expired_pending_appointments(db)` для освобождения просроченных резервов.
   - Шаг 1: Пытается атомарно захватить лок в Redis: `SET lock:slot:{slot_id} {user_id} NX EX 600`. Если ключ уже существует — немедленно возвращает `409 Conflict`.
   - Шаг 2: В транзакции БД проверяет `time_slots.is_available == True` (с `SELECT ... FOR UPDATE`), а также бизнес-правило **«не более одной активной записи (`PENDING` или `CONFIRMED`) к одному врачу у одного пациента»** (при нарушении — `409 Conflict` с освобождением лока). Ставит `is_available = False` и создаёт запись `Appointment` в статусе `PENDING`, `payment_status = UNPAID`.
   - Шаг 3: Отправляет фоновый запрос в `mock-gateway` (`POST /mock/notify`) о создании брони.
   - Возвращает `201 Created` с полем `expires_in_seconds` (текущий TTL ключа лока в Redis).
9. `POST /api/v1/appointments/{id}/confirm` — подтверждение и оплата брони:
   - Проверяет, что запись в статусе `PENDING` и лок `lock:slot:{slot_id}` в Redis ещё не истёк (если истёк — переводит в `CANCELLED`, освобождает слот и возвращает `400 Bad Request`).
   - Вызывает `mock-gateway` (`POST /mock/pay`). При успехе меняет статус на `CONFIRMED`, `payment_status = PAID` и отправляет уведомление через `/mock/notify`.
10. `PATCH /api/v1/appointments/{id}/cancel` — отмена записи:
    - Переводит `status = CANCELLED` (и `payment_status = REFUNDED`, если была оплачена), возвращает `time_slots.is_available = True`, удаляет ключ `lock:slot:{slot_id}` из Redis и вызывает `/mock/notify`.
11. `GET /api/v1/appointments/my` — история записей текущего авторизованного пациента (предварительно вызывает `cleanup_expired_pending_appointments`, для записей `PENDING` возвращает живой `expires_in_seconds` из Redis `TTL`).
12. Прокси-методы Mock-шлюза и симуляции:
    - `GET /api/v1/mock/notifications` — чтение ленты отправленных SMS/Email уведомлений.
    - `GET /api/v1/mock/pay/mode` и `POST /api/v1/mock/pay/mode` — чтение и переключение режима платёжного шлюза (`ok` / `decline` / `error`).
    - `POST /api/v1/mock/expire-locks` — принудительное истечение всех локов `lock:slot:*` в Redis и автоотмена просроченных `PENDING`-броней (симуляция прошествия 10 минут).

## 4. Нефункциональные требования (NFR), фоновые процессы и фронтенд
- Конфигурация строго через переменные окружения (`.env` / `pydantic-settings`): `DATABASE_URL`, `REDIS_URL`, `MOCK_SERVICE_URL`, `JWT_SECRET`. Часовой пояс всех контейнеров и СУБД — `Asia/Almaty` (`UTC+5`).
- Автоматическая очистка просроченных резервов (`cleanup_expired_pending_appointments`): запускается фоновым воркером каждые 5 секунд в `lifespan` приложения, а также при чтении слотов/записей и создании новой брони.
- Логирование всех входящих HTTP-запросов, времени их выполнения (`X-Process-Time-Ms`) и ошибок через middleware.
- Автоматическая документация OpenAPI/Swagger на `/docs`.
- Веб-интерфейс SPA в стиле **Material Design 3 (Material You)** на `GET /` (`app/static/index.html`):
  * Тёмная/светлая темы и 3 цветовые палитры;
  * Быстрое демо-переключение ролей и полноценный MD3 Dialog входа/регистрации;
  * Полноценная Админ-панель (4 вкладки: `+ Тайм-слот`, `+ Врач`, `+ Клиника`, `+ Специализация`);
  * Живой таймер обратного отсчёта резерва (`⏳ Осталось MM:SS` от 600 сек) с автоматическим переводом записи в `CANCELLED` и освобождением слота при достижении `00:00`, а также кнопкой мгновенного сброса TTL в пульте Mock-шлюза.
- Идемпотентный скрипт `app/seed.py` для начального наполнения БД и набор из **18 интеграционных автотестов** (`tests/test_api.py`).
