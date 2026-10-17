# Отчёт по выполнению СРСП: Разработка сервиса онлайн-записи к врачам «MedBook»

---

## 1. Введение и цель работы

Целью работы является проектирование и реализация отказоустойчивого, высоконагруженного сервиса онлайн-записи к врачам **«MedBook»** с соблюдением принципов современной микросервисной архитектуры, теоремы CAP (сочетание AP- и CP-подходов в единой системе), защитой от состояния гонки (Race Condition / Double Booking), кэшированием и контейнеризацией.

---

## 2. Таблица соответствия требованиям спецификации (СРС Часть 1)

| Раздел спецификации | Требование | Статус реализации | Файл в кодовой базе | Примечания и детали |
|---|---|:---:|---|---|
| **1. Стек технологий** | Python 3.12+, FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL 16, Redis 7, httpx, Docker | **Выполнено на 100%** | `requirements.txt`, `Dockerfile`, `docker-compose.yml` | Используется Python 3.12, драйвер `psycopg 3`, Redis 7 и PostgreSQL 16 в Alpine-сборках. |
| **2. Таблицы БД: `users`** | Поля `id`, `email` (unique, idx), `phone`, `full_name`, `role`, `hashed_password`, `created_at` | **Выполнено** | `app/models.py` (L18-35) | Пароли хешируются через `bcrypt`, роли: `patient`, `doctor`, `admin`. |
| **2. Таблицы БД: `clinics`** | Поля `id`, `name`, `city` (idx), `address`, `rating` | **Выполнено** | `app/models.py` (L38-49) | Индекс по полю `city` для быстрой фильтрации по городам. |
| **2. Таблицы БД: `specializations`** | Поля `id`, `name` (unique), `description` | **Выполнено** | `app/models.py` (L52-60) | Уникальное имя специализации. |
| **2. Таблицы БД: `doctors`** | Поля `clinic_id`, `specialization_id`, `full_name`, `experience_years`, `price`, `rating` | **Выполнено** | `app/models.py` (L63-87) | Внешние ключи на `clinics` и `specializations`. |
| **2. Индекс врачей** | Составной индекс `idx_doctors_clinic_spec` `(clinic_id, specialization_id)` | **Выполнено** | `app/models.py` (L65-67), миграция `0001` | Оптимизирует поиск врачей по клинике и специализации. |
| **2. Таблицы БД: `time_slots`** | Поля `doctor_id`, `start_time`, `end_time`, `is_available` | **Выполнено** | `app/models.py` (L90-109) | Связь с таблицей врачей, флаг доступности. |
| **2. Индекс слотов** | Составной индекс `idx_timeslots_doctor_start` `(doctor_id, start_time, is_available)` | **Выполнено** | `app/models.py` (L92-94), миграция `0001` | Гарантирует O(log N) выборку свободных слотов врача на выбранную дату. |
| **2. Таблицы БД: `appointments`** | Поля `user_id`, `doctor_id`, `slot_id`, `status`, `payment_status`, `notes`, `created_at` | **Выполнено** | `app/models.py` (L112-137) | Статусы: `PENDING`, `CONFIRMED`, `CANCELLED`, `COMPLETED`; платежи: `UNPAID`, `PAID`, `REFUNDED`. |
| **2. Индекс записей** | Составной индекс `idx_appointments_user` `(user_id, created_at)` | **Выполнено** | `app/models.py` (L114-116), миграция `0001` | Оптимизирует выборку истории приёмов пациента в хронологическом порядке. |
| **3. API 1: Регистрация** | `POST /api/v1/auth/register` | **Выполнено** | `app/api/auth.py` | Создаёт пользователя с ролью `patient`, хеширует пароль. |
| **3. API 2: Логин** | `POST /api/v1/auth/login` | **Выполнено** | `app/api/auth.py` | Проверяет пароль, генерирует JWT Bearer токен (HS256). |
| **3. API 3: Клиники** | `GET /api/v1/clinics` (`?city=`) и `POST /api/v1/clinics` | **Выполнено** | `app/api/clinics.py` | Публичный просмотр с фильтрацией, создание доступно администраторам. |
| **3. API 4: Специализации** | `GET /api/v1/specializations` и `POST /api/v1/specializations` | **Выполнено** | `app/api/specializations.py` | Каталог специализаций; создание доступно администраторам. |
| **3. API 5: Каталог и создание врачей** | `GET /api/v1/doctors` (`?clinic_id=`, `?specialization_id=`, `?city=`) и `POST /api/v1/doctors` | **Выполнено** | `app/api/doctors.py` | Кэширование в Redis (Cache-Aside, TTL 60 сек). Создание врача админом с инвалидацией `cache:doctors:*`. |
| **3. API 6: Слоты врача** | `GET /api/v1/doctors/{id}/slots` (`?date=`) | **Выполнено** | `app/api/doctors.py` | Автоочистка просроченных броней + кэширование слотов в Redis на 30 секунд. |
| **3. API 7: Добавление слота** | `POST /api/v1/doctors/{id}/slots` | **Выполнено** | `app/api/doctors.py` | Проверка пересечений по времени, инвалидация кэша слотов врача `cache:slots:{id}:*`. |
| **3. API 8: Бронирование (CP)** | `POST /api/v1/appointments` | **Выполнено** | `app/api/appointments.py` | Двухфазная блокировка: Redis `SET NX EX 600` + PostgreSQL `SELECT FOR UPDATE`. Правило «1 активная запись к врачу». Возвращает `expires_in_seconds`. |
| **3. API 9: Подтверждение** | `POST /api/v1/appointments/{id}/confirm` | **Выполнено** | `app/api/appointments.py` | Проверка времени резерва (наличие ключа лока в Redis). Вызов `/mock/pay`, перевод в `CONFIRMED`/`PAID`. |
| **3. API 10: Отмена** | `PATCH /api/v1/appointments/{id}/cancel` | **Выполнено** | `app/api/appointments.py` | Перевод в `CANCELLED`, возврат слота (`is_available = True`), удаление лока, возврат средств (`REFUNDED`). |
| **3. API 11: Мои записи** | `GET /api/v1/appointments/my` | **Выполнено** | `app/api/appointments.py` | Автоочистка истёкших `PENDING`-броней + история приёмов с живым `expires_in_seconds` из Redis `TTL`. |
| **3. API 12: Прокси Mock и TTL** | `GET /api/v1/mock/notifications`, `GET/POST /api/v1/mock/pay/mode`, `POST /api/v1/mock/expire-locks` | **Выполнено** | `app/main.py` | Управление режимами оплаты, лента уведомлений и симуляция истечения 10-минутного резерва слотов. |
| **4. NFR: Конфигурация и TZ** | Строго из окружения / `.env` (`pydantic-settings`), часовой пояс `Asia/Almaty` | **Выполнено** | `app/config.py`, `docker-compose.yml` | `DATABASE_URL`, `REDIS_URL`, `MOCK_SERVICE_URL`, `JWT_SECRET`, `TZ=Asia/Almaty` (`UTC+5`). |
| **4. NFR: Логирование** | Middleware логирования метода, пути, времени выполнения и статуса | **Выполнено** | `app/main.py` | Логирует время каждого запроса и добавляет заголовок `X-Process-Time-Ms`. |
| **4. NFR: Документация** | Swagger UI на `/docs` | **Выполнено** | `app/main.py` | Интерактивная документация с авторизацией Bearer. |
| **4. NFR: Идемпотентный Seed** | Заполнение БД (клиники, врачи, специализации, слоты, пользователи) | **Выполнено** | `app/seed.py` | Заполняет клиники в Алматы, специализации, врачей, слоты и аккаунты `patient@test.kz`, `admin@test.kz`. |

---

## 3. Архитектурные решения: Реализация CAP-теоремы

### 3.1. AP-режим: Каталоги врачей и слотов (Высокая доступность)
- **Паттерн:** Cache-Aside.
- **Поведение:** При входящем запросе проверяется наличие ключа в Redis (`cache:doctors:...`, `cache:slots:...`). При промахе данные читаются из PostgreSQL и сохраняются в Redis с заданным TTL.
- **Отказоустойчивость:** Все методы чтения кэша обёрнуты в безопасный перехват `RedisError`. Если Redis временно выйдет из строя или сеть станет недоступна, каталог не отдаст ошибку 500, а прозрачно переключится на чтение напрямую из PostgreSQL.

### 3.2. CP-режим: Бронирование слотов (Строгая консистентность)
Проблема онлайн-записи к врачам — риск **Double Booking** (двойного бронирования), когда два пациента одновременно пытаются занять один и тот же слот.
В проекте реализована многоуровневая защита:
1. **Уровень 1 (Распределённая блокировка в памяти):**
   - Вызов `SET lock:slot:{slot_id} {user_id} NX EX 600`.
   - Если ключ уже существует, второй запрос немедленно прерывается с кодом `409 Conflict` без создания нагрузки на диск и транзакционную подсистему БД.
   - Если Redis недоступен, запрос на бронирование отклоняется с `503 Service Unavailable`, так как обеспечение согласованности приоритетнее доступности в критической финансово-медицинской транзакции.
2. **Уровень 2 (Пессимистическая блокировка в реляционной БД и бизнес-ограничения):**
   - Внутри ACID-транзакции PostgreSQL выполняется `SELECT * FROM time_slots WHERE id = :slot_id FOR UPDATE`.
   - Проверяется флаг `is_available == True`.
   - Проверяется бизнес-правило **«не более одной действующей записи (`PENDING` или `CONFIRMED`) к одному врачу у одного пациента»** — при попытке создать вторую активную запись к тому же врачу возвращается `409 Conflict` с немедленным освобождением лока нового слота.
   - При любых непредвиденных ошибках в блоке `except` обязательно выполняется rollback транзакции и атомарное освобождение лока в Redis.
3. **Механизм автоматического освобождения брошенных резервов (`cleanup_expired_pending_appointments`):**
   - Если пациент начал бронирование, но не оплатил запись в течение 10 минут (600 сек), TTL ключа `lock:slot:{slot_id}` в Redis истекает.
   - Фоновый асинхронный воркер в `app/main.py` (каждые 5 секунд), а также синхронные вызовы в начале `GET /api/v1/doctors/{id}/slots`, `GET /api/v1/appointments/my` и `POST /api/v1/appointments` автоматически переводят все `PENDING`-записи с истёкшим локом в статус `CANCELLED`, возвращают `slot.is_available = True` и сбрасывают кэш слотов врача в Redis.

---

## 4. Контейнеризация и оркестрация (Docker Compose)

Система развёрнута в виде 4 изолированных сервисов (в едином часовом поясе `Asia/Almaty`, UTC+5):

```
[Client / Browser]
        |
        v :8000
+--------------------------------------------------------------+
| medbook-backend (FastAPI / Uvicorn, TZ=Asia/Almaty)          |
|   - Автоматически запускает `alembic upgrade head`           |
|   - Автоматически выполняет `python -m app.seed`             |
|   - Запускает фоновый воркер автоотмены и сервер Uvicorn     |
+--------------------------------------------------------------+
        |                  |                    |
        | :5432            | :6379              | :8001
        v                  v                    v
+----------------+  +---------------+  +-----------------------+
| medbook-       |  | medbook-redis |  | medbook-mock-gateway  |
| postgres       |  | (Redis 7      |  | (FastAPI Mock Service)|
| (PostgreSQL 16 |  |  Alpine)      |  |   - /mock/pay         |
|  Asia/Almaty)  |  +---------------+  |   - /mock/notify      |
+----------------+                     +-----------------------+
```

### Особенности развёртывания:
1. **Healthchecks и порядок запуска:** Контейнер `backend` зависит от `postgres`, `redis` и `mock-gateway` через условие `service_healthy`. Backend стартует только тогда, когда PostgreSQL готов принимать TCP-соединения (`pg_isready`), Redis отвечает на `ping`, а mock-gateway отвечает на `/health`.
2. **Изоляция портов:** Чтобы избежать конфликтов с локальными базами данных на хосте, порт PostgreSQL проброшен на `5433:5432`, а Redis — на `6380:6379`. Внутри Docker-сети сервисы общаются по стандартным портам (`5432`, `6379`).

---

## 5. Результаты тестирования

Для проверки функционала разработан тестовый модуль `tests/test_api.py`, содержащий **18 комплексных интеграционных тестов** с автоматической очисткой тестовых артефактов (`cleanup_test_artifacts`).

### Сводка выполнения тестов:
```
tests/test_api.py::test_health_check_and_process_time_header PASSED              [  5%]
tests/test_api.py::test_frontend_index_html_serving PASSED                       [ 11%]
tests/test_api.py::test_register_and_login_flow PASSED                           [ 16%]
tests/test_api.py::test_jwt_protected_routes PASSED                              [ 22%]
tests/test_api.py::test_clinics_catalog_and_rbac PASSED                          [ 27%]
tests/test_api.py::test_specializations_catalog_and_unique PASSED                [ 33%]
tests/test_api.py::test_doctors_cache_aside PASSED                               [ 38%]
tests/test_api.py::test_doctors_create_admin_and_cache_invalidation PASSED       [ 44%]
tests/test_api.py::test_slots_caching_and_invalidation_on_creation PASSED        [ 50%]
tests/test_api.py::test_full_booking_lifecycle_success PASSED                    [ 55%]
tests/test_api.py::test_race_condition_concurrent_booking PASSED                 [ 61%]
tests/test_api.py::test_expired_reservation_confirm_fails PASSED                 [ 66%]
tests/test_api.py::test_abandoned_reservation_rebooking_by_another_user PASSED   [ 72%]
tests/test_api.py::test_appointment_cancel_and_slot_freed PASSED                 [ 77%]
tests/test_api.py::test_cancel_confirmed_appointment_triggers_refund PASSED      [ 83%]
tests/test_api.py::test_my_appointments_history_ordering PASSED                  [ 88%]
tests/test_api.py::test_one_active_appointment_per_doctor_limit PASSED           [ 94%]
tests/test_api.py::test_auto_expire_pending_on_read_and_ttl PASSED               [100%]

============================== 18 passed ==============================
```

### Ключевые проверенные сценарии:
1. **Тест отдачи веб-интерфейса (`GET /`):** Проверка возврата `index.html` в кодировке UTF-8 с токенами Material You и кодом 200 OK.
2. **Тест управления каталогом и RBAC (`POST /api/v1/clinics`, `/specializations`, `/doctors`, `/slots`):** Проверка запрета (`403`) для пациента, создания сущностей администратором (`201`) и автоматической инвалидации кэша в Redis.
3. **Тест состояния гонки (Race Condition):** 10 параллельных потоков одновременно пытаются забронировать один и тот же слот через `ThreadPoolExecutor`. Ровно **один** запрос получает ответ `201 Created`, а оставшиеся **девять** получают `409 Conflict`.
4. **Тест лимита 1 активной записи к врачу (`test_one_active_appointment_per_doctor_limit`):** Вторая запись того же пациента к тому же врачу блокируется с `409 Conflict`, запись к другому врачу разрешается (`201 Created`), а после отмены первой записи повторная бронь снова проходит (`201 Created`).
5. **Тест точного TTL и автоотмены при чтении (`test_auto_expire_pending_on_read_and_ttl`):** Проверка `expires_in_seconds > 0` у новой `PENDING`-записи, сброс локов через `POST /api/v1/mock/expire-locks` и автоматический переход записи в `CANCELLED` с освобождением слота при следующем чтении.

---

## 6. Веб-интерфейс в стиле Material Design 3 (Material You)

Реализовано дополнительное требование СРСП — полноценное одностраничное приложение на `GET /` (`app/static/index.html`):
- **Стилизация и темы:** Токены MD3, поддержка Тёмной и Светлой тем, 3 акцентные палитры Material You (Medical Teal, Dynamic Indigo, Sakura Rose).
- **Авторизация и Админ-панель:**
  - 1-клик демо-переключение (Пациент 1, Пациент 2, Админ) и модальное окно MD3 Dialog для входа и регистрации новых аккаунтов;
  - Полноценная Админ-панель из 4 вкладок (`+ Тайм-слот`, `+ Врач`, `+ Клиника`, `+ Специализация`) с мгновенным обновлением каталога и фильтров.
- **Живой таймер резерва и пульт System Design:**
  - В карточках `PENDING`-записей работает посекундный таймер обратного отсчёта `⏳ Осталось MM:SS` (на базе `expires_in_seconds` из Redis `TTL`); при обнулении таймера запись прямо на экране переходит в `CANCELLED`, а слот освобождается;
  - Пульт управления mock-шлюзом позволяет переключать режим оплаты (`200 OK` / `402 Decline` / `502 Error`), в 1 клик сбрасывать TTL резервов (`POST /api/v1/mock/expire-locks`) и отслеживать живую ленту SMS/Email уведомлений.

---

## 7. Заключение

Все требования технической спецификации «MedBook» выполнены в полном объёме:
- Реализована схема из 6 реляционных таблиц с внешними ключами и 3 составными индексами.
- Построены все обязательные и сервисные эндпоинты REST API с валидацией Pydantic v2 и аутентификацией JWT.
- Внедрены паттерны Cache-Aside (AP), Distributed Lock (CP), фоновая автоочистка истёкших резервов и контроль одной активной записи к врачу.
- Развёрнут изолированный mock-сервис для имитации внешних интеграций.
- Разработан и интегрирован веб-интерфейс в строгом стиле Material Design 3 с живым таймером резерва и 4-вкладочной панелью администратора.
- Настроена контейнеризация через Docker Compose (`Asia/Almaty`) с автоматическим применением миграций и сидированием.
- Кодовая база покрыта 18 интеграционными тестами с успешностью прохождения 100%.
