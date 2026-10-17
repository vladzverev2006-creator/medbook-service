"""Integration tests for MedBook API.

Covers authentication, catalogs, Redis caching, full booking lifecycle,
race conditions, lock expiration, abandoned reservation release, and cancellation.
"""

import os
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
import pytest
import redis
from sqlalchemy import create_engine, text

API_BASE_URL = os.getenv("TEST_API_URL", "http://localhost:8000")
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6380/0")
MOCK_SERVICE_URL = os.getenv("TEST_MOCK_URL", "http://localhost:8001")
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg://medbook:medbook@localhost:5433/medbook"
)


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(base_url=API_BASE_URL, timeout=10.0) as c:
        yield c


@pytest.fixture(scope="session")
def r_client() -> redis.Redis:
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    yield r
    r.close()


@pytest.fixture(scope="session")
def mock_client() -> httpx.Client:
    with httpx.Client(base_url=MOCK_SERVICE_URL, timeout=5.0) as c:
        yield c


# Test users are created as "<prefix>_<hex6-8>@test.kz"; seeded/demo accounts
# (patient@test.kz, patient2@test.kz, admin@test.kz) never match this pattern.
TEST_USER_EMAIL_REGEX = r"_[0-9a-f]{6,8}@test\.kz$"


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_artifacts(r_client: redis.Redis, mock_client: httpx.Client):
    """Remove everything this test run created, so the working DB / UI stay clean.

    Snapshot of max ids is taken before the run; at teardown we delete only rows
    that are newer than the snapshot AND match the tests' naming conventions
    (users `*_<hex>@test.kz`, specializations `Спец-*`, clinics `Новая клиника *`),
    plus all slots created after the snapshot. Seeded slots that tests booked are
    made available again. Finally Redis is flushed and the mock log is cleared.
    """
    engine = create_engine(TEST_DATABASE_URL)
    snapshot: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            for table in ("users", "clinics", "doctors", "time_slots"):
                snapshot[table] = conn.execute(
                    text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
                ).scalar_one()
    except Exception as exc:  # DB not reachable from the host -> cleanup impossible
        warnings.warn(f"Test cleanup disabled, cannot reach database: {exc}")
        engine = None

    yield

    if engine is not None:
        with engine.begin() as conn:
            user_ids = [
                row[0]
                for row in conn.execute(
                    text("SELECT id FROM users WHERE id > :snap AND email ~ :rx"),
                    {"snap": snapshot["users"], "rx": TEST_USER_EMAIL_REGEX},
                )
            ]
            if user_ids:
                booked_slot_ids = [
                    row[0]
                    for row in conn.execute(
                        text("SELECT DISTINCT slot_id FROM appointments WHERE user_id = ANY(:ids)"),
                        {"ids": user_ids},
                    )
                ]
                conn.execute(
                    text("DELETE FROM appointments WHERE user_id = ANY(:ids)"),
                    {"ids": user_ids},
                )
                # Seeded slots that tests booked become available again
                # (slots created by tests are deleted below).
                if booked_slot_ids:
                    conn.execute(
                        text(
                            "UPDATE time_slots SET is_available = TRUE "
                            "WHERE id = ANY(:ids) AND id <= :snap"
                        ),
                        {"ids": booked_slot_ids, "snap": snapshot["time_slots"]},
                    )
                conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": user_ids})

            conn.execute(
                text(
                    "DELETE FROM time_slots WHERE id > :snap AND NOT EXISTS "
                    "(SELECT 1 FROM appointments a WHERE a.slot_id = time_slots.id)"
                ),
                {"snap": snapshot["time_slots"]},
            )
            conn.execute(
                text(
                    "DELETE FROM doctors WHERE id > :snap AND full_name LIKE 'Тестовый Врач %' "
                    "AND NOT EXISTS (SELECT 1 FROM time_slots ts WHERE ts.doctor_id = doctors.id) "
                    "AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.doctor_id = doctors.id)"
                ),
                {"snap": snapshot["doctors"]},
            )
            conn.execute(
                text(
                    "DELETE FROM clinics WHERE id > :snap AND name LIKE 'Новая клиника %' "
                    "AND NOT EXISTS (SELECT 1 FROM doctors d WHERE d.clinic_id = clinics.id)"
                ),
                {"snap": snapshot["clinics"]},
            )
            conn.execute(
                text(
                    "DELETE FROM specializations WHERE name LIKE 'Спец-%' "
                    "AND NOT EXISTS (SELECT 1 FROM doctors d WHERE d.specialization_id = specializations.id)"
                )
            )
        engine.dispose()

    r_client.flushdb()
    try:
        mock_client.delete("/mock/notifications")
        mock_client.post("/mock/pay/mode", json={"mode": "ok"})
    except httpx.HTTPError as exc:
        warnings.warn(f"Could not reset mock gateway: {exc}")


@pytest.fixture
def clean_mock(mock_client: httpx.Client):
    mock_client.post("/mock/pay/mode", json={"mode": "ok"})
    mock_client.delete("/mock/notifications")
    yield mock_client
    mock_client.post("/mock/pay/mode", json={"mode": "ok"})


@pytest.fixture
def admin_headers(client: httpx.Client) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@test.kz", "password": "password123"},
    )
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def patient_headers(client: httpx.Client) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "patient@test.kz", "password": "password123"},
    )
    assert resp.status_code == 200, f"Patient login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def create_new_user(client: httpx.Client, prefix: str = "user") -> tuple[dict[str, str], dict]:
    unique = uuid.uuid4().hex[:8]
    email = f"{prefix}_{unique}@test.kz"
    payload = {
        "email": email,
        "phone": "+77011234567",
        "full_name": f"Test {prefix.capitalize()} {unique}",
        "password": "password123",
    }
    reg = client.post("/api/v1/auth/register", json=payload)
    assert reg.status_code == 201, reg.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, reg.json()


def get_or_create_slot(client: httpx.Client, admin_headers: dict[str, str], doctor_id: int = 1) -> dict:
    slots = client.get(f"/api/v1/doctors/{doctor_id}/slots").json()
    now_cutoff = datetime.now() + timedelta(minutes=5)
    for s in slots:
        if s["is_available"] and datetime.fromisoformat(s["start_time"]) > now_cutoff:
            return s
    # Create a fresh slot in the future if none available
    unique_day = 30 + (uuid.uuid4().int % 5000)
    start = datetime.now() + timedelta(days=unique_day, hours=10)
    resp = client.post(
        f"/api/v1/doctors/{doctor_id}/slots",
        json={
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=30)).isoformat(),
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# 1. Health & Middleware & Frontend
# --------------------------------------------------------------------------
def test_health_check_and_process_time_header(client: httpx.Client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["components"]["database"] == "ok"
    assert data["components"]["redis"] == "ok"
    assert "x-process-time-ms" in resp.headers


def test_frontend_index_html_serving(client: httpx.Client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "MedBook" in resp.text
    assert "Material You" in resp.text


# --------------------------------------------------------------------------
# 2. Authentication & Authorization
# --------------------------------------------------------------------------
def test_register_and_login_flow(client: httpx.Client):
    email = f"pat_{uuid.uuid4().hex[:6]}@TEST.kz"
    reg_resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "phone": "+77015554433",
            "full_name": "Тестовый Пациент",
            "password": "password123",
        },
    )
    assert reg_resp.status_code == 201
    user_data = reg_resp.json()
    assert user_data["email"] == email.lower()
    assert user_data["role"] == "patient"
    assert "id" in user_data

    # Duplicate registration -> 409
    dup_resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email.lower(),
            "phone": "+77015554433",
            "full_name": "Тестовый Пациент",
            "password": "password123",
        },
    )
    assert dup_resp.status_code == 409

    # Password validation: bcrypt max length (UTF-8 > 72 bytes) -> 422
    long_pwd_resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"invalid_{uuid.uuid4().hex[:6]}@test.kz",
            "phone": "+77015554433",
            "full_name": "Long Password",
            "password": "пароль" * 15,  # >72 bytes in UTF-8
        },
    )
    assert long_pwd_resp.status_code == 422

    # Login with wrong password -> 401
    bad_login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrongpassword"},
    )
    assert bad_login.status_code == 401

    # Login with correct password -> Bearer token
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert login_resp.status_code == 200
    token_data = login_resp.json()
    assert "access_token" in token_data
    assert token_data["token_type"].lower() == "bearer"


def test_jwt_protected_routes(client: httpx.Client):
    # No auth header -> 401
    assert client.get("/api/v1/appointments/my").status_code == 401
    # Invalid token -> 401
    assert (
        client.get(
            "/api/v1/appointments/my",
            headers={"Authorization": "Bearer invalid.token.value"},
        ).status_code
        == 401
    )


# --------------------------------------------------------------------------
# 3. Clinics & Specializations
# --------------------------------------------------------------------------
def test_clinics_catalog_and_rbac(
    client: httpx.Client, patient_headers: dict[str, str], admin_headers: dict[str, str]
):
    # Public get
    resp = client.get("/api/v1/clinics", params={"city": "Алматы"})
    assert resp.status_code == 200
    clinics = resp.json()
    assert len(clinics) >= 2
    assert all(c["city"] == "Алматы" for c in clinics)

    # Empty city search
    assert client.get("/api/v1/clinics", params={"city": "UnknownCity"}).json() == []

    # Patient cannot create clinic -> 403
    clinic_body = {
        "name": f"Новая клиника {uuid.uuid4().hex[:4]}",
        "city": "Астана",
        "address": "ул. Сыганак, 10",
        "rating": 4.5,
    }
    assert (
        client.post("/api/v1/clinics", json=clinic_body, headers=patient_headers).status_code
        == 403
    )

    # Admin can create clinic -> 201
    create_resp = client.post(
        "/api/v1/clinics", json=clinic_body, headers=admin_headers
    )
    assert create_resp.status_code == 201
    assert create_resp.json()["name"] == clinic_body["name"]


def test_specializations_catalog_and_unique(
    client: httpx.Client, patient_headers: dict[str, str], admin_headers: dict[str, str]
):
    resp = client.get("/api/v1/specializations")
    assert resp.status_code == 200
    specs = resp.json()
    assert len(specs) >= 3

    # Patient cannot create -> 403
    spec_name = f"Спец-{uuid.uuid4().hex[:6]}"
    assert (
        client.post(
            "/api/v1/specializations",
            json={"name": spec_name, "description": "Desc"},
            headers=patient_headers,
        ).status_code
        == 403
    )

    # Admin creates -> 201
    created = client.post(
        "/api/v1/specializations",
        json={"name": spec_name, "description": "Desc"},
        headers=admin_headers,
    )
    assert created.status_code == 201

    # Duplicate specialization -> 409
    dup = client.post(
        "/api/v1/specializations",
        json={"name": spec_name, "description": "Another desc"},
        headers=admin_headers,
    )
    assert dup.status_code == 409


# --------------------------------------------------------------------------
# 4. Doctors & Slots with Redis Caching
# --------------------------------------------------------------------------
def test_doctors_cache_aside(client: httpx.Client, r_client: redis.Redis):
    r_client.flushdb()
    cache_key = "cache:doctors:clinic=any:spec=any:city=Алматы"
    assert not r_client.exists(cache_key)

    # First request: DB miss -> sets cache in Redis (TTL <= 60)
    resp1 = client.get("/api/v1/doctors", params={"city": "Алматы"})
    assert resp1.status_code == 200
    docs1 = resp1.json()
    assert len(docs1) >= 3

    assert r_client.exists(cache_key)
    ttl = r_client.ttl(cache_key)
    assert 0 < ttl <= 60

    # Second request: Cache hit -> exact same data
    resp2 = client.get("/api/v1/doctors", params={"city": "Алматы"})
    assert resp2.status_code == 200
    assert resp2.json() == docs1


def test_doctors_create_admin_and_cache_invalidation(
    client: httpx.Client,
    admin_headers: dict[str, str],
    patient_headers: dict[str, str],
    r_client: redis.Redis,
):
    # Populate doctors cache first
    client.get("/api/v1/doctors", params={"city": "Алматы"})
    assert r_client.keys("cache:doctors:*")

    doc_payload = {
        "full_name": f"Тестовый Врач {uuid.uuid4().hex[:6]}",
        "clinic_id": 1,
        "specialization_id": 1,
        "experience_years": 7,
        "price": "14000.00",
        "rating": 4.9,
    }

    # Patient cannot create doctor -> 403
    assert (
        client.post("/api/v1/doctors", json=doc_payload, headers=patient_headers).status_code
        == 403
    )

    # Non-existent clinic -> 404
    assert (
        client.post(
            "/api/v1/doctors",
            json={**doc_payload, "clinic_id": 999999},
            headers=admin_headers,
        ).status_code
        == 404
    )

    # Non-existent specialization -> 404
    assert (
        client.post(
            "/api/v1/doctors",
            json={**doc_payload, "specialization_id": 999999},
            headers=admin_headers,
        ).status_code
        == 404
    )

    # Admin creates doctor -> 201 and invalidates doctors cache in Redis
    create_resp = client.post("/api/v1/doctors", json=doc_payload, headers=admin_headers)
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["full_name"] == doc_payload["full_name"]
    assert created["clinic_id"] == 1
    assert created["specialization_id"] == 1
    assert not r_client.keys("cache:doctors:*")


def test_slots_caching_and_invalidation_on_creation(
    client: httpx.Client,
    admin_headers: dict[str, str],
    patient_headers: dict[str, str],
    r_client: redis.Redis,
):
    doctor_id = 1
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    cache_key = f"cache:slots:{doctor_id}:{tomorrow}"

    # Read slots -> populates cache (TTL <= 30)
    resp = client.get(f"/api/v1/doctors/{doctor_id}/slots", params={"date": tomorrow})
    assert resp.status_code == 200
    assert r_client.exists(cache_key)
    assert 0 < r_client.ttl(cache_key) <= 30

    # Patient cannot create slot -> 403
    unique_day = 50 + (uuid.uuid4().int % 5000)
    future_start = datetime.now() + timedelta(days=unique_day, hours=10)
    slot_payload = {
        "start_time": future_start.isoformat(),
        "end_time": (future_start + timedelta(minutes=30)).isoformat(),
    }
    assert (
        client.post(
            f"/api/v1/doctors/{doctor_id}/slots",
            json=slot_payload,
            headers=patient_headers,
        ).status_code
        == 403
    )

    # Admin creates slot -> 201 and invalidates all cache keys for this doctor
    create_resp = client.post(
        f"/api/v1/doctors/{doctor_id}/slots",
        json=slot_payload,
        headers=admin_headers,
    )
    assert create_resp.status_code == 201
    assert not r_client.keys(f"cache:slots:{doctor_id}:*")

    # Overlapping slot -> 409
    overlap_resp = client.post(
        f"/api/v1/doctors/{doctor_id}/slots",
        json=slot_payload,
        headers=admin_headers,
    )
    assert overlap_resp.status_code == 409

    # Invalid slot (end_time <= start_time) -> 422
    bad_time_resp = client.post(
        f"/api/v1/doctors/{doctor_id}/slots",
        json={
            "start_time": slot_payload["end_time"],
            "end_time": slot_payload["start_time"],
        },
        headers=admin_headers,
    )
    assert bad_time_resp.status_code == 422


# --------------------------------------------------------------------------
# 5. Full Booking Lifecycle (Create -> Lock -> Confirm & Pay)
# --------------------------------------------------------------------------
def test_full_booking_lifecycle_success(
    client: httpx.Client,
    admin_headers: dict[str, str],
    clean_mock: httpx.Client,
    r_client: redis.Redis,
):
    user_headers, user = create_new_user(client, "lifecycle")
    slot = get_or_create_slot(client, admin_headers, doctor_id=2)
    slot_id = slot["id"]

    # Step 1: Create appointment
    create_resp = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_id, "notes": "Плановый осмотр"},
        headers=user_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    appt = create_resp.json()
    assert appt["status"] == "PENDING"
    assert appt["payment_status"] == "UNPAID"
    assert appt["slot_id"] == slot_id

    # Redis lock exists with TTL <= 600
    lock_key = f"lock:slot:{slot_id}"
    assert r_client.get(lock_key) == str(user["id"])
    assert 0 < r_client.ttl(lock_key) <= 600

    # Step 2: Confirm and pay
    confirm_resp = client.post(
        f"/api/v1/appointments/{appt['id']}/confirm",
        headers=user_headers,
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    confirmed = confirm_resp.json()
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["payment_status"] == "PAID"

    # Confirming again -> 400 (already confirmed)
    assert (
        client.post(
            f"/api/v1/appointments/{appt['id']}/confirm",
            headers=user_headers,
        ).status_code
        == 400
    )


# --------------------------------------------------------------------------
# 6. Race Condition (Double Booking Protection)
# --------------------------------------------------------------------------
def test_race_condition_concurrent_booking(
    client: httpx.Client,
    admin_headers: dict[str, str],
    r_client: redis.Redis,
):
    """Simulates 10 concurrent patients booking the exact same slot at the same moment."""
    slot = get_or_create_slot(client, admin_headers, doctor_id=3)
    slot_id = slot["id"]

    users = [create_new_user(client, f"race_{i}") for i in range(10)]

    def book(user_tuple):
        headers, _ = user_tuple
        return client.post(
            "/api/v1/appointments",
            json={"slot_id": slot_id},
            headers=headers,
        ).status_code

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(book, users))

    # Exactly ONE client must succeed with 201, all others must receive 409
    assert results.count(201) == 1, f"Expected 1 success, got: {results}"
    assert results.count(409) == 9, f"Expected 9 conflicts, got: {results}"

    # Verify lock in Redis belongs to the winner
    assert r_client.exists(f"lock:slot:{slot_id}")


# --------------------------------------------------------------------------
# 7. Lock Expiration & Abandoned Reservation Release
# --------------------------------------------------------------------------
def test_expired_reservation_confirm_fails(
    client: httpx.Client,
    admin_headers: dict[str, str],
    r_client: redis.Redis,
):
    user_headers, _ = create_new_user(client, "expired")
    slot = get_or_create_slot(client, admin_headers, doctor_id=1)
    slot_id = slot["id"]

    appt = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_id},
        headers=user_headers,
    ).json()

    # Simulate TTL expiration in Redis
    r_client.delete(f"lock:slot:{slot_id}")

    # Confirm attempt after lock expiration must fail with 400
    resp = client.post(
        f"/api/v1/appointments/{appt['id']}/confirm",
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert "истекло" in resp.json()["detail"].lower()


def test_abandoned_reservation_rebooking_by_another_user(
    client: httpx.Client,
    admin_headers: dict[str, str],
    r_client: redis.Redis,
):
    """If user 1 leaves a slot in PENDING and the Redis lock expires,

    user 2 must be able to book this slot (user 1's pending appointment is CANCELLED).
    """
    user1_headers, user1 = create_new_user(client, "abandoner")
    user2_headers, user2 = create_new_user(client, "new_patient")
    slot = get_or_create_slot(client, admin_headers, doctor_id=1)
    slot_id = slot["id"]

    # User 1 creates booking
    appt1 = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_id},
        headers=user1_headers,
    ).json()
    assert appt1["status"] == "PENDING"

    # User 1 abandons: Redis lock expires
    r_client.delete(f"lock:slot:{slot_id}")

    # User 2 books the same slot
    resp2 = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_id},
        headers=user2_headers,
    )
    assert resp2.status_code == 201, resp2.text
    appt2 = resp2.json()
    assert appt2["status"] == "PENDING"
    assert appt2["user_id"] == user2["id"]

    # Verify user 1's appointment was transitioned to CANCELLED
    my_appts1 = client.get("/api/v1/appointments/my", headers=user1_headers).json()
    old_appt = next(a for a in my_appts1 if a["id"] == appt1["id"])
    assert old_appt["status"] == "CANCELLED"


# --------------------------------------------------------------------------
# 8. Cancellation Flow
# --------------------------------------------------------------------------
def test_appointment_cancel_and_slot_freed(
    client: httpx.Client,
    admin_headers: dict[str, str],
    r_client: redis.Redis,
):
    user1_headers, _ = create_new_user(client, "canceler")
    user2_headers, _ = create_new_user(client, "booker")
    slot = get_or_create_slot(client, admin_headers, doctor_id=2)
    slot_id = slot["id"]

    appt = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_id},
        headers=user1_headers,
    ).json()

    # Cancel pending appointment
    cancel_resp = client.patch(
        f"/api/v1/appointments/{appt['id']}/cancel",
        headers=user1_headers,
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "CANCELLED"

    # Redis lock is removed
    assert not r_client.exists(f"lock:slot:{slot_id}")

    # Another user can immediately book the freed slot
    rebook_resp = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_id},
        headers=user2_headers,
    )
    assert rebook_resp.status_code == 201


def test_cancel_confirmed_appointment_triggers_refund(
    client: httpx.Client,
    admin_headers: dict[str, str],
    clean_mock: httpx.Client,
):
    user_headers, _ = create_new_user(client, "refunder")
    slot = get_or_create_slot(client, admin_headers, doctor_id=3)

    appt = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot["id"]},
        headers=user_headers,
    ).json()

    # Confirm
    client.post(f"/api/v1/appointments/{appt['id']}/confirm", headers=user_headers)

    # Cancel
    resp = client.patch(
        f"/api/v1/appointments/{appt['id']}/cancel",
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert resp.json()["payment_status"] == "REFUNDED"


# --------------------------------------------------------------------------
# 9. Appointment History, 1-Active-Per-Doctor Rule & Auto-Expire TTL
# --------------------------------------------------------------------------
def test_my_appointments_history_ordering(
    client: httpx.Client,
    patient_headers: dict[str, str],
):
    resp = client.get("/api/v1/appointments/my", headers=patient_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    if len(data) > 1:
        ids = [a["id"] for a in data]
        assert ids == sorted(ids, reverse=True)


def test_one_active_appointment_per_doctor_limit(
    client: httpx.Client,
    admin_headers: dict[str, str],
):
    user_headers, _ = create_new_user(client, "onedoc")
    slot1_doc1 = get_or_create_slot(client, admin_headers, doctor_id=1)

    # First booking with doctor 1 -> 201 Created
    resp1 = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot1_doc1["id"], "notes": "Первая запись"},
        headers=user_headers,
    )
    assert resp1.status_code == 201, resp1.text
    appt1 = resp1.json()

    # Second booking with the SAME doctor 1 (different slot) -> 409 Conflict
    slot2_doc1 = get_or_create_slot(client, admin_headers, doctor_id=1)
    assert slot2_doc1["id"] != slot1_doc1["id"]
    resp_dup = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot2_doc1["id"], "notes": "Вторая запись к тому же врачу"},
        headers=user_headers,
    )
    assert resp_dup.status_code == 409
    assert "уже есть действующая запись к этому врачу" in resp_dup.json()["detail"].lower()

    # Booking with a DIFFERENT doctor (doctor 2) -> 201 Created
    slot_doc2 = get_or_create_slot(client, admin_headers, doctor_id=2)
    resp_doc2 = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_doc2["id"], "notes": "Запись к другому врачу"},
        headers=user_headers,
    )
    assert resp_doc2.status_code == 201, resp_doc2.text

    # Cancel first appointment with doctor 1 -> 200 OK
    cancel_resp = client.patch(
        f"/api/v1/appointments/{appt1['id']}/cancel",
        headers=user_headers,
    )
    assert cancel_resp.status_code == 200

    # Now booking with doctor 1 again succeeds -> 201 Created
    resp_rebook = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot2_doc1["id"], "notes": "Повторная запись после отмены"},
        headers=user_headers,
    )
    assert resp_rebook.status_code == 201, resp_rebook.text


def test_auto_expire_pending_on_read_and_ttl(
    client: httpx.Client,
    admin_headers: dict[str, str],
):
    user_headers, _ = create_new_user(client, "ttl_expire")
    slot = get_or_create_slot(client, admin_headers, doctor_id=1)
    slot_id = slot["id"]

    # Create PENDING reservation -> expires_in_seconds must be > 0 and <= 600
    create_resp = client.post(
        "/api/v1/appointments",
        json={"slot_id": slot_id, "notes": "Проверка TTL и автоотмены"},
        headers=user_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    appt = create_resp.json()
    assert appt["status"] == "PENDING"
    assert appt["expires_in_seconds"] is not None
    assert 0 < appt["expires_in_seconds"] <= 600

    # GET /api/v1/appointments/my also returns live expires_in_seconds > 0
    my_before = client.get("/api/v1/appointments/my", headers=user_headers).json()
    current = next(a for a in my_before if a["id"] == appt["id"])
    assert current["status"] == "PENDING"
    assert current["expires_in_seconds"] is not None and current["expires_in_seconds"] > 0

    # Trigger lock expiration via POST /api/v1/mock/expire-locks
    expire_resp = client.post("/api/v1/mock/expire-locks")
    assert expire_resp.status_code == 200, expire_resp.text
    assert expire_resp.json()["cancelled"] >= 1

    # Ordinary GET /api/v1/appointments/my now shows status CANCELLED and expires_in_seconds None
    my_after = client.get("/api/v1/appointments/my", headers=user_headers).json()
    expired_appt = next(a for a in my_after if a["id"] == appt["id"])
    assert expired_appt["status"] == "CANCELLED"
    assert expired_appt["expires_in_seconds"] is None

    # And the slot is available again in GET /api/v1/doctors/1/slots
    slots_after = client.get("/api/v1/doctors/1/slots").json()
    freed_slot = next(s for s in slots_after if s["id"] == slot_id)
    assert freed_slot["is_available"] is True
