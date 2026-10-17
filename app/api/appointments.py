"""Appointments: the CP part of the system (Redis lock + SELECT ... FOR UPDATE)."""

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Appointment, Doctor, TimeSlot, User
from app.redis_client import (
    acquire_slot_lock,
    get_slot_lock_owner,
    get_slot_lock_ttl,
    invalidate_cache,
    release_slot_lock,
    slots_cache_pattern,
)
from app.schemas import AppointmentCreate, AppointmentOut
from app.services import external
from app.services.external import PaymentDeclined, PaymentGatewayUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/appointments", tags=["appointments"])

APPOINTMENT_NOT_FOUND = "Запись не найдена"
ONE_ACTIVE_PER_DOCTOR_DETAIL = (
    "У вас уже есть действующая запись к этому врачу. "
    "Завершите или отмените текущую запись перед созданием новой."
)


def _lock_appointment(db: Session, appointment_id: int) -> Appointment | None:
    return db.scalar(
        select(Appointment).where(Appointment.id == appointment_id).with_for_update()
    )


def _lock_slot(db: Session, slot_id: int) -> TimeSlot | None:
    return db.scalar(select(TimeSlot).where(TimeSlot.id == slot_id).with_for_update())


def _attach_ttl(appointment: Appointment) -> Appointment:
    if appointment.status == "PENDING":
        appointment.expires_in_seconds = get_slot_lock_ttl(appointment.slot_id)
    else:
        appointment.expires_in_seconds = None
    return appointment


def cleanup_expired_pending_appointments(db: Session) -> int:
    """Cancel PENDING appointments whose Redis lock has expired and free their slots."""
    pending_list = list(
        db.scalars(select(Appointment).where(Appointment.status == "PENDING"))
    )
    if not pending_list:
        return 0

    cancelled_count = 0
    affected_doctors: set[int] = set()
    try:
        for appt in pending_list:
            owner = get_slot_lock_owner(appt.slot_id)
            if owner is None:
                appt.status = "CANCELLED"
                slot = db.get(TimeSlot, appt.slot_id)
                other_active = db.scalar(
                    select(Appointment.id)
                    .where(
                        Appointment.slot_id == appt.slot_id,
                        Appointment.id != appt.id,
                        Appointment.status.in_(("PENDING", "CONFIRMED")),
                    )
                    .limit(1)
                )
                if slot is not None and other_active is None:
                    slot.is_available = True
                affected_doctors.add(appt.doctor_id)
                cancelled_count += 1
        if cancelled_count > 0:
            db.commit()
    except RedisError as exc:
        db.rollback()
        logger.warning("Redis unavailable during pending cleanup, skipping: %s", exc)
        return 0
    except Exception as exc:
        db.rollback()
        logger.warning("Error during pending cleanup: %s", exc)
        return 0

    for doc_id in affected_doctors:
        invalidate_cache(slots_cache_pattern(doc_id))
    return cancelled_count


def _notify(tasks: BackgroundTasks, user: User, event: str, message: str, appointment_id: int) -> None:
    """Notification is sent after the response; failures are only logged."""
    tasks.add_task(
        external.notify,
        user_id=user.id,
        event=event,
        message=message,
        appointment_id=appointment_id,
        email=user.email,
        phone=user.phone,
    )


@router.post("", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
def create_appointment(
    payload: AppointmentCreate,
    tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Appointment:
    slot_id = payload.slot_id
    cleanup_expired_pending_appointments(db)

    # Step 1: distributed lock. RedisError -> 503 (fail closed, see app.main).
    if not acquire_slot_lock(slot_id, user.id, ttl=settings.SLOT_LOCK_TTL):
        if get_slot_lock_owner(slot_id) == user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, ONE_ACTIVE_PER_DOCTOR_DETAIL)
        raise HTTPException(status.HTTP_409_CONFLICT, "Слот уже бронируется другим пациентом")

    # Step 2: DB transaction with row lock. Any failure releases our Redis lock.
    try:
        slot = _lock_slot(db, slot_id)
        if slot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Слот не найден")
        if not slot.is_available:
            pending_appt = db.scalar(
                select(Appointment)
                .where(
                    Appointment.slot_id == slot_id,
                    Appointment.status == "PENDING",
                )
                .with_for_update()
            )
            lock_owner = get_slot_lock_owner(slot_id)
            if pending_appt is not None and (
                lock_owner is None
                or (lock_owner == user.id and pending_appt.user_id != user.id)
            ):
                pending_appt.status = "CANCELLED"
                slot.is_available = True
                if lock_owner is None and not acquire_slot_lock(
                    slot_id, user.id, ttl=settings.SLOT_LOCK_TTL
                ):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "Слот уже бронируется другим пациентом",
                    )
            else:
                raise HTTPException(status.HTTP_409_CONFLICT, "Слот уже занят")

        # Business rule: at most 1 active (PENDING or CONFIRMED) appointment per doctor per patient
        existing_active = db.scalar(
            select(Appointment.id)
            .where(
                Appointment.user_id == user.id,
                Appointment.doctor_id == slot.doctor_id,
                Appointment.status.in_(("PENDING", "CONFIRMED")),
            )
            .limit(1)
        )
        if existing_active is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, ONE_ACTIVE_PER_DOCTOR_DETAIL)

        if slot.start_time <= datetime.now():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя записаться на прошедшее время")

        slot.is_available = False
        appointment = Appointment(
            user_id=user.id,
            doctor_id=slot.doctor_id,
            slot_id=slot.id,
            status="PENDING",
            payment_status="UNPAID",
            notes=payload.notes,
        )
        db.add(appointment)
        db.commit()
    except Exception:
        db.rollback()
        release_slot_lock(slot_id, owner_id=user.id)
        raise

    db.refresh(appointment)
    invalidate_cache(slots_cache_pattern(slot.doctor_id))

    # Step 3: notification (non-blocking for booking).
    _notify(
        tasks, user, "appointment_created",
        f"Слот забронирован на {slot.start_time:%d.%m.%Y %H:%M}. "
        f"Подтвердите и оплатите запись в течение {settings.SLOT_LOCK_TTL // 60} минут.",
        appointment.id,
    )
    return _attach_ttl(appointment)


@router.post("/{appointment_id}/confirm", response_model=AppointmentOut)
def confirm_appointment(
    appointment_id: int,
    tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Appointment:
    appointment = _lock_appointment(db, appointment_id)
    if appointment is None or appointment.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, APPOINTMENT_NOT_FOUND)
    if appointment.status != "PENDING":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Подтвердить можно только запись в статусе PENDING (текущий: {appointment.status})",
        )

    # The reservation is valid only while our Redis lock is alive.
    if get_slot_lock_owner(appointment.slot_id) != appointment.user_id:
        slot = _lock_slot(db, appointment.slot_id)
        appointment.status = "CANCELLED"
        if slot is not None:
            slot.is_available = True
        db.commit()
        if slot is not None:
            invalidate_cache(slots_cache_pattern(slot.doctor_id))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Время резерва истекло")

    doctor = db.get(Doctor, appointment.doctor_id)
    try:
        external.pay(appointment_id=appointment.id, user_id=user.id, amount=doctor.price)
    except PaymentDeclined as exc:
        db.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, f"Платёж отклонён: {exc}")
    except PaymentGatewayUnavailable:
        db.rollback()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Платёжный сервис недоступен, попробуйте позже"
        )

    appointment.status = "CONFIRMED"
    appointment.payment_status = "PAID"
    db.commit()
    db.refresh(appointment)

    _notify(tasks, user, "appointment_confirmed", "Запись подтверждена и оплачена.", appointment.id)
    return appointment


@router.patch("/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel_appointment(
    appointment_id: int,
    tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Appointment:
    appointment = _lock_appointment(db, appointment_id)
    if appointment is None or (appointment.user_id != user.id and user.role != "admin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, APPOINTMENT_NOT_FOUND)
    if appointment.status not in ("PENDING", "CONFIRMED"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Нельзя отменить запись в статусе {appointment.status}",
        )

    slot = _lock_slot(db, appointment.slot_id)
    appointment.status = "CANCELLED"
    if appointment.payment_status == "PAID":
        appointment.payment_status = "REFUNDED"
    if slot is not None:
        slot.is_available = True
    db.commit()
    db.refresh(appointment)

    release_slot_lock(appointment.slot_id, owner_id=appointment.user_id)
    invalidate_cache(slots_cache_pattern(appointment.doctor_id))

    owner = user if appointment.user_id == user.id else db.get(User, appointment.user_id)
    _notify(tasks, owner, "appointment_cancelled", "Запись отменена.", appointment.id)
    return appointment


@router.get("/my", response_model=list[AppointmentOut])
def my_appointments(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Appointment]:
    cleanup_expired_pending_appointments(db)
    stmt = (
        select(Appointment)
        .where(Appointment.user_id == user.id)
        .order_by(Appointment.created_at.desc(), Appointment.id.desc())
    )
    return [_attach_ttl(a) for a in db.scalars(stmt)]
