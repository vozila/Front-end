# services/user_service.py
import os
from sqlalchemy.orm import Session
from models import User

def get_or_create_primary_user(db: Session) -> User:
    """
    Creates/returns the primary admin user using ADMIN_EMAIL.
    This matches how Vozlia works in the main backend.
    """
    email = os.getenv("ADMIN_EMAIL")
    if not email:
        raise RuntimeError("ADMIN_EMAIL is not set")

    user = db.query(User).filter(User.email == email).first()
    if user:
        return user

    user = User(email=email)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
