"""Idempotent seed script: `python -m app.seed`."""

from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Clinic, Doctor, Specialization, TimeSlot, User
from app.security import hash_password

PASSWORD = "password123"

USERS = [
    {
        "email": "patient@test.kz",
        "phone": "+77010000001",
        "full_name": "Test Patient",
        "role": "patient",
    },
    {
        "email": "admin@test.kz",
        "phone": "+77010000002",
        "full_name": "Test Admin",
        "role": "admin",
    },
]

CLINICS = [
    {"name": "Медицинский центр «Умай»", "city": "Алматы",
     "address": "ул. Достык, 117", "rating": 4.8},
    {"name": "Клиника «Сункар»", "city": "Алматы",
     "address": "пр. Абая, 150", "rating": 4.6},
]

SPECIALIZATIONS = [
    {"name": "Терапевт", "description": "Первичный приём, диагностика и лечение"},
    {"name": "Кардиолог", "description": "Заболевания сердца и сосудов"},
    {"name": "Невролог", "description": "Заболевания нервной системы"},
]

# (full_name, clinic name, specialization name, experience_years, price, rating)
DOCTORS = [
    ("Айгерим Сериккызы Нурпеисова", "Медицинский центр «Умай»", "Терапевт", 8, "8000.00", 4.9),
    ("Данияр Маратович Ахметов", "Медицинский центр «Умай»", "Кардиолог", 15, "15000.00", 4.8),
    ("Светлана Игоревна Ким", "Клиника «Сункар»", "Невролог", 11, "12000.00", 4.7),
]

SLOT_DAYS_AHEAD = 3  # starting from tomorrow
SLOT_START_TIMES = [time(9, 0), time(10, 0), time(11, 0), time(14, 0)]
SLOT_DURATION = timedelta(minutes=30)


def seed_users(db: Session) -> None:
    for data in USERS:
        if db.scalar(select(User).where(User.email == data["email"])) is None:
            db.add(User(**data, hashed_password=hash_password(PASSWORD)))
    db.flush()


def seed_clinics(db: Session) -> dict[str, Clinic]:
    result: dict[str, Clinic] = {}
    for data in CLINICS:
        clinic = db.scalar(
            select(Clinic).where(
                Clinic.name == data["name"], Clinic.city == data["city"]
            )
        )
        if clinic is None:
            clinic = Clinic(**data)
            db.add(clinic)
        result[data["name"]] = clinic
    db.flush()
    return result


def seed_specializations(db: Session) -> dict[str, Specialization]:
    result: dict[str, Specialization] = {}
    for data in SPECIALIZATIONS:
        spec = db.scalar(
            select(Specialization).where(Specialization.name == data["name"])
        )
        if spec is None:
            spec = Specialization(**data)
            db.add(spec)
        result[data["name"]] = spec
    db.flush()
    return result


def seed_doctors(
    db: Session,
    clinics: dict[str, Clinic],
    specs: dict[str, Specialization],
) -> list[Doctor]:
    doctors: list[Doctor] = []
    for full_name, clinic_name, spec_name, exp, price, rating in DOCTORS:
        clinic, spec = clinics[clinic_name], specs[spec_name]
        doctor = db.scalar(
            select(Doctor).where(
                Doctor.full_name == full_name, Doctor.clinic_id == clinic.id
            )
        )
        if doctor is None:
            doctor = Doctor(
                clinic_id=clinic.id,
                specialization_id=spec.id,
                full_name=full_name,
                experience_years=exp,
                price=Decimal(price),
                rating=rating,
            )
            db.add(doctor)
        doctors.append(doctor)
    db.flush()
    return doctors


def seed_slots(db: Session, doctors: list[Doctor]) -> None:
    now = datetime.now()
    # Clean up unbooked slots in the past so the catalog always starts with future slots
    for stale in db.scalars(
        select(TimeSlot).where(
            TimeSlot.start_time <= now,
            ~TimeSlot.appointments.any(),
        )
    ):
        db.delete(stale)
    db.flush()

    today = now.date()
    for doctor in doctors:
        for offset in range(1, SLOT_DAYS_AHEAD + 1):
            day = today + timedelta(days=offset)
            for start_t in SLOT_START_TIMES:
                start = datetime.combine(day, start_t)
                exists = db.scalar(
                    select(TimeSlot.id).where(
                        TimeSlot.doctor_id == doctor.id,
                        TimeSlot.start_time == start,
                    )
                )
                if exists is None:
                    db.add(
                        TimeSlot(
                            doctor_id=doctor.id,
                            start_time=start,
                            end_time=start + SLOT_DURATION,
                            is_available=True,
                        )
                    )
    db.flush()


def run() -> None:
    with SessionLocal() as db:
        seed_users(db)
        clinics = seed_clinics(db)
        specs = seed_specializations(db)
        doctors = seed_doctors(db, clinics, specs)
        seed_slots(db, doctors)
        db.commit()
    print("Seed completed.")


if __name__ == "__main__":
    run()
