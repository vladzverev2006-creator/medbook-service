from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.models import Clinic
from app.schemas import ClinicCreate, ClinicOut

router = APIRouter(prefix="/clinics", tags=["clinics"])


@router.get("", response_model=list[ClinicOut])
def list_clinics(city: str | None = None, db: Session = Depends(get_db)) -> list[Clinic]:
    stmt = select(Clinic).order_by(Clinic.id)
    if city:
        stmt = stmt.where(Clinic.city == city)
    return list(db.scalars(stmt))


@router.post(
    "",
    response_model=ClinicOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_clinic(payload: ClinicCreate, db: Session = Depends(get_db)) -> Clinic:
    clinic = Clinic(**payload.model_dump())
    db.add(clinic)
    db.commit()
    db.refresh(clinic)
    return clinic
