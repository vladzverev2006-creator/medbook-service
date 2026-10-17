from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

UserRole = Literal["patient", "doctor", "admin"]
AppointmentStatus = Literal["PENDING", "CONFIRMED", "CANCELLED", "COMPLETED"]
PaymentStatus = Literal["UNPAID", "PAID", "REFUNDED"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Auth / Users ----------
class UserRegister(BaseModel):
    email: EmailStr
    phone: str = Field(min_length=5, max_length=32)
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Пароль не должен превышать 72 байта в UTF-8")
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(ORMModel):
    id: int
    email: EmailStr
    phone: str
    full_name: str
    role: UserRole
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Clinics ----------
class ClinicCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    city: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1, max_length=255)
    rating: float = Field(default=5.0, ge=0, le=5)


class ClinicOut(ORMModel):
    id: int
    name: str
    city: str
    address: str
    rating: float


# ---------- Specializations ----------
class SpecializationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class SpecializationOut(ORMModel):
    id: int
    name: str
    description: str | None = None


# ---------- Doctors ----------
class DoctorCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    clinic_id: int = Field(gt=0)
    specialization_id: int = Field(gt=0)
    experience_years: int = Field(default=1, ge=0, le=80)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    rating: float = Field(default=5.0, ge=0, le=5)


class DoctorOut(ORMModel):
    id: int
    clinic_id: int
    specialization_id: int
    full_name: str
    experience_years: int
    price: Decimal
    rating: float


# ---------- Time slots ----------
class TimeSlotCreate(BaseModel):
    start_time: datetime
    end_time: datetime

    @model_validator(mode="after")
    def check_order(self) -> "TimeSlotCreate":
        if (self.start_time.tzinfo is None) != (self.end_time.tzinfo is None):
            raise ValueError("start_time и end_time должны быть в одном формате (оба с часовым поясом или оба без)")
        if self.end_time <= self.start_time:
            raise ValueError("end_time должен быть позже start_time")
        return self


class TimeSlotOut(ORMModel):
    id: int
    doctor_id: int
    start_time: datetime
    end_time: datetime
    is_available: bool


# ---------- Appointments ----------
class AppointmentCreate(BaseModel):
    slot_id: int
    notes: str | None = None


class AppointmentOut(ORMModel):
    id: int
    user_id: int
    doctor_id: int
    slot_id: int
    status: AppointmentStatus
    payment_status: PaymentStatus
    notes: str | None = None
    created_at: datetime
    expires_in_seconds: int | None = None


AppointmentRead = AppointmentOut
