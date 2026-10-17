from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(
        String, nullable=False, default="patient", server_default="patient"
    )  # patient | doctor | admin
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, server_default=func.now()
    )

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="user")


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str] = mapped_column(String, index=True, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)
    rating: Mapped[float] = mapped_column(Float, default=5.0, server_default="5.0")

    doctors: Mapped[list["Doctor"]] = relationship(back_populates="clinic")


class Specialization(Base):
    __tablename__ = "specializations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)

    doctors: Mapped[list["Doctor"]] = relationship(back_populates="specialization")


class Doctor(Base):
    __tablename__ = "doctors"
    __table_args__ = (
        Index("idx_doctors_clinic_spec", "clinic_id", "specialization_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"), nullable=False)
    specialization_id: Mapped[int] = mapped_column(
        ForeignKey("specializations.id"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    experience_years: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    rating: Mapped[float] = mapped_column(Float, default=5.0, server_default="5.0")

    clinic: Mapped["Clinic"] = relationship(back_populates="doctors")
    specialization: Mapped["Specialization"] = relationship(back_populates="doctors")
    slots: Mapped[list["TimeSlot"]] = relationship(back_populates="doctor")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="doctor")


class TimeSlot(Base):
    __tablename__ = "time_slots"
    __table_args__ = (
        Index("idx_timeslots_doctor_start", "doctor_id", "start_time", "is_available"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    doctor: Mapped["Doctor"] = relationship(back_populates="slots")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="slot")


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (Index("idx_appointments_user", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    slot_id: Mapped[int] = mapped_column(ForeignKey("time_slots.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING", server_default="PENDING"
    )  # PENDING | CONFIRMED | CANCELLED | COMPLETED
    payment_status: Mapped[str] = mapped_column(
        String, nullable=False, default="UNPAID", server_default="UNPAID"
    )  # UNPAID | PAID | REFUNDED
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="appointments")
    doctor: Mapped["Doctor"] = relationship(back_populates="appointments")
    slot: Mapped["TimeSlot"] = relationship(back_populates="appointments")
