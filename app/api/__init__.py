from fastapi import APIRouter

from app.api import appointments, auth, clinics, doctors, specializations

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(clinics.router)
api_router.include_router(specializations.router)
api_router.include_router(doctors.router)
api_router.include_router(appointments.router)
