from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.appointments import cleanup_expired_pending_appointments
from app.auth import require_admin
from app.config import settings
from app.database import get_db
from app.models import Clinic, Doctor, Specialization, TimeSlot
from app.redis_client import (
    doctors_cache_key,
    doctors_cache_pattern,
    get_cache,
    invalidate_cache,
    set_cache,
    slots_cache_key,
    slots_cache_pattern,
)
from app.schemas import DoctorCreate, DoctorOut, TimeSlotCreate, TimeSlotOut

router = APIRouter(prefix="/doctors", tags=["doctors"])

DOCTOR_NOT_FOUND = "Врач не найден"


@router.get("", response_model=list[DoctorOut])
def list_doctors(
    clinic_id: int | None = Query(None, gt=0),
    specialization_id: int | None = Query(None, gt=0),
    city: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    """Cache-aside, TTL = DOCTORS_CACHE_TTL (60s). Falls back to DB if Redis is down."""
    key = doctors_cache_key(clinic_id, specialization_id, city)
    cached = get_cache(key)
    if cached is not None:
        return cached

    stmt = select(Doctor).order_by(Doctor.id)
    if clinic_id is not None:
        stmt = stmt.where(Doctor.clinic_id == clinic_id)
    if specialization_id is not None:
        stmt = stmt.where(Doctor.specialization_id == specialization_id)
    if city:
        stmt = stmt.join(Clinic, Doctor.clinic_id == Clinic.id).where(Clinic.city == city)

    data = [DoctorOut.model_validate(d).model_dump(mode="json") for d in db.scalars(stmt)]
    set_cache(key, data, settings.DOCTORS_CACHE_TTL)
    return data


@router.post(
    "",
    response_model=DoctorOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_doctor(payload: DoctorCreate, db: Session = Depends(get_db)) -> Doctor:
    if db.get(Clinic, payload.clinic_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Клиника не найдена")
    if db.get(Specialization, payload.specialization_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Специализация не найдена")

    doctor = Doctor(
        full_name=payload.full_name,
        clinic_id=payload.clinic_id,
        specialization_id=payload.specialization_id,
        experience_years=payload.experience_years,
        price=payload.price,
        rating=payload.rating,
    )
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    invalidate_cache(doctors_cache_pattern())
    return doctor


@router.get("/{doctor_id}/slots", response_model=list[TimeSlotOut])
def list_slots(
    doctor_id: int,
    day: date | None = Query(None, alias="date", description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
) -> Any:
    """Cache-aside, TTL = SLOTS_CACHE_TTL (30s). Falls back to DB if Redis is down."""
    cleanup_expired_pending_appointments(db)
    key = slots_cache_key(doctor_id, day)
    cached = get_cache(key)
    if cached is not None:
        return cached

    if db.get(Doctor, doctor_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, DOCTOR_NOT_FOUND)

    stmt = select(TimeSlot).where(TimeSlot.doctor_id == doctor_id).order_by(TimeSlot.start_time)
    if day is not None:
        day_start = datetime.combine(day, time.min)
        stmt = stmt.where(
            TimeSlot.start_time >= day_start,
            TimeSlot.start_time < day_start + timedelta(days=1),
        )

    data = [TimeSlotOut.model_validate(s).model_dump(mode="json") for s in db.scalars(stmt)]
    set_cache(key, data, settings.SLOTS_CACHE_TTL)
    return data


@router.post(
    "/{doctor_id}/slots",
    response_model=TimeSlotOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_slot(doctor_id: int, payload: TimeSlotCreate, db: Session = Depends(get_db)) -> TimeSlot:
    # Lock the doctor row to serialize concurrent slot creation for the same doctor.
    doctor = db.scalar(select(Doctor).where(Doctor.id == doctor_id).with_for_update())
    if doctor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, DOCTOR_NOT_FOUND)

    overlap = db.scalar(
        select(TimeSlot.id)
        .where(
            TimeSlot.doctor_id == doctor_id,
            TimeSlot.start_time < payload.end_time,
            TimeSlot.end_time > payload.start_time,
        )
        .limit(1)
    )
    if overlap is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Слот пересекается с существующим слотом врача")

    slot = TimeSlot(
        doctor_id=doctor_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        is_available=True,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    invalidate_cache(slots_cache_pattern(doctor_id))
    return slot
