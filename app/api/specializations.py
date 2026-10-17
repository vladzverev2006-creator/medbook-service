from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.models import Specialization
from app.schemas import SpecializationCreate, SpecializationOut

router = APIRouter(prefix="/specializations", tags=["specializations"])

NAME_TAKEN = "Специализация с таким названием уже существует"


@router.get("", response_model=list[SpecializationOut])
def list_specializations(db: Session = Depends(get_db)) -> list[Specialization]:
    return list(db.scalars(select(Specialization).order_by(Specialization.name)))


@router.post(
    "",
    response_model=SpecializationOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_specialization(
    payload: SpecializationCreate, db: Session = Depends(get_db)
) -> Specialization:
    if db.scalar(select(Specialization.id).where(Specialization.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, NAME_TAKEN)
    spec = Specialization(**payload.model_dump())
    db.add(spec)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, NAME_TAKEN)
    db.refresh(spec)
    return spec
