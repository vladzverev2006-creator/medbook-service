![CI/CD Pipeline](https://img.shields.io/badge/CI%2FCD-GitHub_Actions_Passing-brightgreen)

# MedBook — Сервис онлайн-записи к врачам

Высоконагруженный сервис онлайн-записи к врачам с защитой от двойного бронирования (Double Booking), двухуровневой моделью консистентности (CAP: AP для каталога и CP для бронирования), кэшированием в Redis и интеграцией с внешними платёжными и SMS/Email-шлюзами.

---

## 1. Архитектура и стек технологий

```
                           +-------------------------------------+
                           |            Клиент / Web             |
                           +-------------------------------------+
                                              |
                                              v (HTTP / REST, JWT Bearer)
+----------------------------------------------------------------------------------------+
|                                    Docker Network                                      |
|                                                                                        |
|   +-----------------------+     Cache-Aside (AP)      +----------------------------+   |
|   |                       |<------------------------->|       Redis 7 (Cache)      |   |
|   |                       |                           +----------------------------+   |
|   |                       |     Distributed Lock (CP) +----------------------------+   |
|   |                       |<------------------------->|   Redis 7 (Lock SET NX EX) |   |
|   |   FastAPI Backend     |                           +----------------------------+   |
|   |      (Port 8000)      |                                                            |
|   |                       |     SELECT FOR UPDATE     +----------------------------+   |
|   |                       |<------------------------->|      PostgreSQL 16         |   |
|   |                       |                           +----------------------------+   |
|   |                       |                                                            |
|   |                       |     POST /mock/pay        +----------------------------+   |
|   |                       |-------------------------->|        Mock-Gateway        |   |
|   |                       |     POST /mock/notify     |    (Платежи и Уведомления) |   |
|   |                       |-------------------------->|        (Port 8001)         |   |
|   +-----------------------+                           +----------------------------+   |
+----------------------------------------------------------------------------------------+
```

### Стек:
- **Язык & Фреймворк:** Python 3.12+, FastAPI, Starlette, Uvicorn
- **ORM & База данных:** SQLAlchemy 2.0 (psycopg 3), PostgreSQL 16
- **Миграции:** Alembic
- **Валидация:** Pydantic v2 & Pydantic-Settings
- **Кэширование & Блокировки:** Redis 7 (паттерн Cache-Aside + Distributed Lock `SET NX EX 600`)
- **Аутентификация:** JWT (`PyJWT`), хеширование паролей (`bcrypt`)
- **Внешние вызовы:** `httpx` (таймаут 2.0 сек, устойчивость к сбоям)
- **Контейнеризация:** Docker & Docker Compose (4 сервиса)
- **Тестирование:** `pytest` + `httpx` (интеграционные тесты и гонки бронирования)

### Реализация CAP-теоремы в коде:
1. **AP (Высокая доступность каталога):**
   - Каталог врачей (`GET /api/v1/doctors`) кэшируется в Redis на 60 секунд.
   - Слоты врачей (`GET /api/v1/doctors/{id}/slots`) кэшируются на 30 секунд.
   - При недоступности или сбое Redis система логирует предупреждение и прозрачно считывает данные из PostgreSQL, не прерывая работу пользователей.
2. **CP (Строгая согласованность при бронировании слотов):**
   - Бронирование слота (`POST /api/v1/appointments`) требует захвата атомарного распределённого лока `SET lock:slot:{slot_id} {user_id} NX EX 600`.
   - Внутри транзакции PostgreSQL выполняется пессимистическая блокировка строки `SELECT ... FOR UPDATE` таблицы `time_slots`.
   - Если Redis недоступен, бронирование прерывается с `503 Service Unavailable`, предотвращая риск двойного бронирования (Double Booking).
   - **Ограничение 1 активной записи к врачу:** у одного пациента не может быть более одной действующей записи (`PENDING` или `CONFIRMED`) к одному и тому же врачу одновременно (`409 Conflict`).
   - **Автоматическая отмена просроченных резервов:** фоновый воркер (каждые 5 секунд в `lifespan`), а также хуки при чтении слотов/записей (`cleanup_expired_pending_appointments`) проверяют наличие живого лока в Redis и при истечении 600-секундного TTL автоматически переводят `PENDING`-запись в `CANCELLED`, возвращая слот в свободные (`is_available = True`).

---

## 2. Быстрый запуск через Docker Compose

### Предварительные требования:
- Установленные `docker` и `docker compose`.

### Запуск системы:
```bash
# Клонирование и переход в папку проекта
cd medbook-service

# Запуск всех 4 контейнеров с автоматической сборкой, миграциями и заполнением БД (seed)
docker compose up -d --build
```

### Проверка статуса сервисов:
```bash
docker compose ps
```
Вывод покажет 4 здоровых (healthy) контейнера (все работают в часовом поясе `Asia/Almaty`, UTC+5):
- `medbook-backend` (порт `8000:8000`)
- `medbook-mock-gateway` (порт `8001:8001`)
- `medbook-postgres` (проброс порта `5433:5432` на хост, `timezone=Asia/Almaty`)
- `medbook-redis` (проброс порта `6380:6379` на хост)

### Проверка работоспособности:
```bash
curl http://localhost:8000/health
# {"status":"ok","components":{"database":"ok","redis":"ok"}}
```

---

## 3. Переменные окружения

Конфигурация централизована в `app/config.py` через `pydantic-settings`:

| Переменная | Описание | Значение в Docker | Значение по умолчанию |
|---|---|---|---|
| `DATABASE_URL` | Строка подключения к PostgreSQL | `postgresql+psycopg://medbook:medbook@postgres:5432/medbook` | Обязательная |
| `REDIS_URL` | Адрес сервера Redis | `redis://redis:6379/0` | `redis://localhost:6379/0` |
| `MOCK_SERVICE_URL` | Адрес mock-шлюза платежей/SMS | `http://mock-gateway:8001` | `http://localhost:8001` |
| `JWT_SECRET` | Секретный ключ для подписи JWT | Строка (например, `medbook-super-secret-jwt-key-2026`) | Обязательная |
| `JWT_ALGORITHM` | Алгоритм подписи токена | `HS256` | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Время жизни JWT токена | `60` | `60` |
| `DOCTORS_CACHE_TTL` | Время жизни кэша врачей (сек) | `60` | `60` |
| `SLOTS_CACHE_TTL` | Время жизни кэша слотов (сек) | `30` | `30` |
| `SLOT_LOCK_TTL` | Время удержания брони в Redis (сек) | `600` (10 мин) | `600` |
| `EXTERNAL_TIMEOUT` | Таймаут вызова внешних сервисов (сек)| `2.0` | `2.0` |

---

## 4. Веб-интерфейс в стиле Material Design 3 (Material You)

В проект встроен полноценный одностраничный SPA веб-интерфейс, доступный по адресу:
👉 **[http://localhost:8000/](http://localhost:8000/)**

### Возможности интерфейса:
1. **Дизайн-система Material Design 3:**
   - Полноценная поддержка **Тёмной** (по умолчанию) и **Светлой** темы;
   - Переключение 3 динамических палитр Material You:
     * **Medical Teal** (бирюзово-циановая медицинская тема)
     * **Dynamic Indigo** (глубокая индиго-синяя палитра)
     * **Sakura Rose** (элегантная палитра нежной розы)
   - Геометрия MD3: карточки с плавным скруглением `24px–28px` на тональных поверхностях (Tonal Surfaces), фильтр-чипы (Filter Chips) с анимацией выбора, пилюльные кнопки (Pill Buttons) и адаптивные MD3 Snackbars.
2. **Top App Bar, Демо-логин в 1 клик и MD3 Dialog авторизации:**
   - Индикатор здоровья системы `/health` с живым статусом базы данных и Redis;
   - Чип задержки последнего HTTP-запроса в миллисекундах (`X-Process-Time-Ms`);
   - Модальное окно **«Аккаунт / Регистрация»** (MD3 Dialog) с вкладками «Вход» и «Регистрация» + кнопка «Выход»;
   - Быстрое переключение демо-пользователей без ручного ввода паролей:
     * **Пациент 1 (`patient@test.kz`)** — моментальный вход;
     * **Пациент 2 (`patient2@test.kz`)** — автоматическая регистрация/вход для живой демонстрации параллельного бронирования и конфликтов;
     * **Админ (`admin@test.kz`)** — вход с активацией панели администратора.
3. **Полноценная Админ-панель (4 вкладки MD3 Tabs):**
   - **«+ Тайм-слот»** (`POST /api/v1/doctors/{id}/slots`) — добавление слота врачу в локальном времени Алматы со сбросом кэша слотов в Redis;
   - **«+ Врач»** (`POST /api/v1/doctors`) — добавление нового врача с выбором клиники и специализации и инвалидацией кэша каталога `cache:doctors:*`;
   - **«+ Клиника»** (`POST /api/v1/clinics`) — создание клиники с динамическим обновлением чипсов городов;
   - **«+ Специализация»** (`POST /api/v1/specializations`) — создание специализации с мгновенным обновлением фильтр-чипсов.
4. **Каталог врачей и расписание:**
   - Интерактивные фильтры по городам и специализациям;
   - Карточки врачей со стажем, рейтингом, клиникой и ценой приёма;
   - Сетка тайм-слотов:
     * **Свободные слоты:** интерактивные чипы для мгновенной брони (`POST /api/v1/appointments`);
     * **Занятые слоты:** защищены от бронирования, при клике можно проверить работу защиты от Race Condition (всплывает MD3 Snackbar с `409 Conflict`).
5. **Управление записями пациента и живой таймер обратного отсчёта:**
   - Просмотр всех записей (`GET /api/v1/appointments/my`) с отображением точного времени приёма (слота) и времени создания в часовом поясе Алматы;
   - Живой бейдж обратного отсчёта **`⏳ Осталось MM:SS`** для записей в статусе `PENDING`, синхронизированный с реальным `TTL` ключа в Redis (`expires_in_seconds`);
   - При достижении таймером `00:00` интерфейс без перезагрузки страницы автоматически обновляет карточку (статус меняется с `PENDING` на `CANCELLED`), а слот в расписании снова становится свободным;
   - Кнопки быстрой онлайн-оплаты через mock-шлюз и отмены с автоматическим возвратом слота в расписание.
6. **Пульт управления Mock-шлюзом (System Design Demo):**
   - Переключатель режимов платёжного шлюза: `200 OK` (успех), `402 Payment Required` (отказ карты), `502 Bad Gateway` (сбой банка);
   - Демо-кнопка **«⏱ Сбросить TTL резервов (симуляция 10 мин)»** (`POST /api/v1/mock/expire-locks`) для мгновенной демонстрации истечения резерва и автоотмены `PENDING`-записей;
   - Живая лента отправленных SMS/Email уведомлений с автоматическим обновлением.

---

## 5. Интерактивная документация (Swagger & ReDoc)

После запуска системы доступны веб-интерфейсы:
- **Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **OpenAPI JSON:** [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

В правом верхнем углу Swagger UI нажмите кнопку **Authorize** и укажите токен `Bearer <ваш_jwt_токен>` для тестирования защищённых эндпоинтов.

---

## 6. Запуск тестов

В репозитории подготовлен полный набор из **18 интеграционных тестов** `tests/test_api.py`, тестирующий сценарии авторизации, веб-интерфейса, создания сущностей админом, кэширования, состояния гонки (Race Condition при одновременном бронировании), лимита одной активной записи к врачу, автоотмены просроченных резервов и возврата средств. Фикстура `cleanup_test_artifacts` автоматически очищает БД и Redis после прогона.

```bash
# Активация виртуального окружения
source .venv/bin/activate

# Запуск тестов pytest
pytest -v tests/test_api.py
```

Результат:
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

---

## 7. Примеры curl-запросов для эндпоинтов API

Предустановленные тестовые пользователи (пароль `password123`):
- Пациент: `patient@test.kz`
- Администратор: `admin@test.kz`

### 1. Регистрация нового пациента (`POST /api/v1/auth/register`)
```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.kz",
    "phone": "+77011234567",
    "full_name": "Иван Иванов",
    "password": "password123"
  }'
```

### 2. Аутентификация и получение JWT (`POST /api/v1/auth/login`)
```bash
# Логин пациента
PATIENT_TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"patient@test.kz","password":"password123"}' | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)

# Логин администратора
ADMIN_TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@test.kz","password":"password123"}' | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)

echo "Patient Token: $PATIENT_TOKEN"
echo "Admin Token: $ADMIN_TOKEN"
```

### 3. Получение списка клиник (`GET /api/v1/clinics`)
```bash
curl -s "http://localhost:8000/api/v1/clinics?city=Алматы"
```

### 4. Создание клиники (`POST /api/v1/clinics`, только для Admin)
```bash
curl -X POST http://localhost:8000/api/v1/clinics \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Медицинский центр Керуен",
    "city": "Астана",
    "address": "пр. Мангилик Ел, 24",
    "rating": 4.9
  }'
```

### 5. Получение специализаций (`GET /api/v1/specializations`)
```bash
curl -s http://localhost:8000/api/v1/specializations
```

### 6. Создание специализации (`POST /api/v1/specializations`, только для Admin)
```bash
curl -X POST http://localhost:8000/api/v1/specializations \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Офтальмолог",
    "description": "Диагностика и лечение заболеваний глаз"
  }'
```

### 7. Поиск и создание врачей (`GET /api/v1/doctors`, `POST /api/v1/doctors`)
```bash
# Поиск с фильтрацией по клинике, специализации или городу (кэш на 60 сек)
curl -s "http://localhost:8000/api/v1/doctors?city=Алматы&specialization_id=1"

# Добавление нового врача (только для Admin, инвалидирует кэш cache:doctors:*)
curl -X POST http://localhost:8000/api/v1/doctors \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "full_name": "Сатпаев Арман Болатович",
    "clinic_id": 1,
    "specialization_id": 1,
    "experience_years": 9,
    "price": "15000.00",
    "rating": 4.9
  }'
```

### 8. Получение слотов врача с кэшированием (`GET /api/v1/doctors/{id}/slots`)
```bash
# Получение слотов врача (кэш на 30 сек, фильтр по дате опционален)
curl -s "http://localhost:8000/api/v1/doctors/1/slots?date=2026-10-18"
```

### 9. Добавление слота в расписание (`POST /api/v1/doctors/{id}/slots`, только для Admin)
```bash
curl -X POST http://localhost:8000/api/v1/doctors/1/slots \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "start_time": "2026-10-25T15:00:00",
    "end_time": "2026-10-25T15:30:00"
  }'
```

### 10. Создание бронирования — ключевой CP-узел (`POST /api/v1/appointments`)
```bash
curl -X POST http://localhost:8000/api/v1/appointments \
  -H "Authorization: Bearer $PATIENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slot_id": 1,
    "notes": "Жалобы на головную боль"
  }'
# Ответ: 201 Created (статус PENDING, payment_status UNPAID, expires_in_seconds: 600)
```

### 11. Подтверждение и оплата записи (`POST /api/v1/appointments/{id}/confirm`)
```bash
curl -X POST http://localhost:8000/api/v1/appointments/1/confirm \
  -H "Authorization: Bearer $PATIENT_TOKEN"
```

### 12. Отмена записи (`PATCH /api/v1/appointments/{id}/cancel`)
```bash
curl -X PATCH http://localhost:8000/api/v1/appointments/1/cancel \
  -H "Authorization: Bearer $PATIENT_TOKEN"
```

### 13. История записей авторизованного пациента (`GET /api/v1/appointments/my`)
```bash
curl -s http://localhost:8000/api/v1/appointments/my \
  -H "Authorization: Bearer $PATIENT_TOKEN"
```

### 14. Прокси Mock-шлюза и симуляция истечения TTL (`POST /api/v1/mock/expire-locks`)
```bash
# Сбросить все локи резервов в Redis и автоматически отменить PENDING-записи
curl -X POST http://localhost:8000/api/v1/mock/expire-locks

# Переключить режим оплаты (ok | decline | error)
curl -X POST http://localhost:8000/api/v1/mock/pay/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "ok"}'

# Получить лог уведомлений
curl -s http://localhost:8000/api/v1/mock/notifications
```

---

## 8. Остановка проекта

```bash
docker compose down
```
Для очистки БД и томов данных:
```bash
docker compose down -v
```
