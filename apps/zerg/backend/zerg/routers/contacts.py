"""User contacts routes for approved external action recipients.

Users maintain approved contacts lists that fiches can send to.
This prevents abuse (spam, phishing) while keeping the platform usable.
"""

import logging
import re
from datetime import datetime
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import UserEmailContact
from zerg.models.models import UserPhoneContact

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contacts"], dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_email(email: str) -> str:
    """Normalize email for comparison. Returns lowercase, trimmed.

    Strips display name if present: "Jane <jane@example.com>" -> "jane@example.com"
    """
    email = email.strip().lower()
    # Strip display name if present
    if "<" in email and ">" in email:
        email = email.split("<")[1].split(">")[0].strip().lower()
    return email


def normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format.

    Strips non-digit characters except leading +.
    """
    phone = phone.strip()
    if phone.startswith("+"):
        # Keep + prefix, strip everything else except digits
        return "+" + re.sub(r"[^\d]", "", phone[1:])
    else:
        # No + prefix, just strip non-digits
        digits = re.sub(r"[^\d]", "", phone)
        # Assume US if 10 digits without +
        if len(digits) == 10:
            return "+1" + digits
        return "+" + digits


def validate_email_format(email: str) -> bool:
    """Basic email format validation."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def validate_phone_format(phone: str) -> bool:
    """Basic phone format validation (E.164)."""
    pattern = r"^\+\d{10,15}$"
    return bool(re.match(pattern, phone))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EmailContactCreate(BaseModel):
    """Schema for creating an email contact."""

    name: str = Field(..., min_length=1, max_length=100, description="Contact name")
    email: str = Field(..., min_length=5, max_length=255, description="Email address")
    notes: Optional[str] = Field(None, max_length=500, description="Optional notes")


class EmailContactUpdate(BaseModel):
    """Schema for updating an email contact."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    email: Optional[str] = Field(None, min_length=5, max_length=255)
    notes: Optional[str] = Field(None, max_length=500)


class EmailContactOut(BaseModel):
    """Schema for email contact response."""

    id: int
    name: str
    email: str
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PhoneContactCreate(BaseModel):
    """Schema for creating a phone contact."""

    name: str = Field(..., min_length=1, max_length=100, description="Contact name")
    phone: str = Field(..., min_length=10, max_length=20, description="Phone number (E.164 format)")
    notes: Optional[str] = Field(None, max_length=500, description="Optional notes")


class PhoneContactUpdate(BaseModel):
    """Schema for updating a phone contact."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    phone: Optional[str] = Field(None, min_length=10, max_length=20)
    notes: Optional[str] = Field(None, max_length=500)


class PhoneContactOut(BaseModel):
    """Schema for phone contact response."""

    id: int
    name: str
    phone: str
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Email Contacts Endpoints
# ---------------------------------------------------------------------------


@router.get("/user/contacts/email", response_model=List[EmailContactOut])
def list_email_contacts(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all approved email contacts for the current user."""
    contacts = db.query(UserEmailContact).filter(UserEmailContact.owner_id == current_user.id).order_by(UserEmailContact.name).all()
    return contacts


@router.post("/user/contacts/email", response_model=EmailContactOut, status_code=status.HTTP_201_CREATED)
def create_email_contact(
    contact: EmailContactCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add a new approved email contact."""
    # Normalize email
    email_normalized = normalize_email(contact.email)

    # Validate format
    if not validate_email_format(email_normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid email format: {contact.email}",
        )

    try:
        db_contact = UserEmailContact(
            owner_id=current_user.id,
            name=contact.name.strip(),
            email=contact.email.strip(),
            email_normalized=email_normalized,
            notes=contact.notes.strip() if contact.notes else None,
        )
        db.add(db_contact)
        db.commit()
        db.refresh(db_contact)
        logger.info(f"Created email contact {db_contact.id} for user {current_user.id}")
        return db_contact
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email contact '{email_normalized}' already exists",
        )


@router.put("/user/contacts/email/{contact_id}", response_model=EmailContactOut)
def update_email_contact(
    contact_id: int,
    update: EmailContactUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update an existing email contact."""
    db_contact = (
        db.query(UserEmailContact)
        .filter(
            UserEmailContact.id == contact_id,
            UserEmailContact.owner_id == current_user.id,
        )
        .first()
    )

    if not db_contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Email contact {contact_id} not found",
        )

    # Apply updates
    if update.name is not None:
        db_contact.name = update.name.strip()
    if update.email is not None:
        email_normalized = normalize_email(update.email)
        if not validate_email_format(email_normalized):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid email format: {update.email}",
            )
        db_contact.email = update.email.strip()
        db_contact.email_normalized = email_normalized
    if update.notes is not None:
        db_contact.notes = update.notes.strip() if update.notes else None

    try:
        db.commit()
        db.refresh(db_contact)
        logger.info(f"Updated email contact {contact_id} for user {current_user.id}")
        return db_contact
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already exists for another contact",
        )


@router.delete("/user/contacts/email/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_email_contact(
    contact_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete an email contact."""
    db_contact = (
        db.query(UserEmailContact)
        .filter(
            UserEmailContact.id == contact_id,
            UserEmailContact.owner_id == current_user.id,
        )
        .first()
    )

    if not db_contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Email contact {contact_id} not found",
        )

    db.delete(db_contact)
    db.commit()
    logger.info(f"Deleted email contact {contact_id} for user {current_user.id}")


# ---------------------------------------------------------------------------
# Phone Contacts Endpoints
# ---------------------------------------------------------------------------


@router.get("/user/contacts/phone", response_model=List[PhoneContactOut])
def list_phone_contacts(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all approved phone contacts for the current user."""
    contacts = db.query(UserPhoneContact).filter(UserPhoneContact.owner_id == current_user.id).order_by(UserPhoneContact.name).all()
    return contacts


@router.post("/user/contacts/phone", response_model=PhoneContactOut, status_code=status.HTTP_201_CREATED)
def create_phone_contact(
    contact: PhoneContactCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add a new approved phone contact."""
    # Normalize phone
    phone_normalized = normalize_phone(contact.phone)

    # Validate format
    if not validate_phone_format(phone_normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid phone format: {contact.phone}. Use E.164 format (e.g., +14155552671)",
        )

    try:
        db_contact = UserPhoneContact(
            owner_id=current_user.id,
            name=contact.name.strip(),
            phone=contact.phone.strip(),
            phone_normalized=phone_normalized,
            notes=contact.notes.strip() if contact.notes else None,
        )
        db.add(db_contact)
        db.commit()
        db.refresh(db_contact)
        logger.info(f"Created phone contact {db_contact.id} for user {current_user.id}")
        return db_contact
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Phone contact '{phone_normalized}' already exists",
        )


@router.put("/user/contacts/phone/{contact_id}", response_model=PhoneContactOut)
def update_phone_contact(
    contact_id: int,
    update: PhoneContactUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update an existing phone contact."""
    db_contact = (
        db.query(UserPhoneContact)
        .filter(
            UserPhoneContact.id == contact_id,
            UserPhoneContact.owner_id == current_user.id,
        )
        .first()
    )

    if not db_contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Phone contact {contact_id} not found",
        )

    # Apply updates
    if update.name is not None:
        db_contact.name = update.name.strip()
    if update.phone is not None:
        phone_normalized = normalize_phone(update.phone)
        if not validate_phone_format(phone_normalized):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid phone format: {update.phone}",
            )
        db_contact.phone = update.phone.strip()
        db_contact.phone_normalized = phone_normalized
    if update.notes is not None:
        db_contact.notes = update.notes.strip() if update.notes else None

    try:
        db.commit()
        db.refresh(db_contact)
        logger.info(f"Updated phone contact {contact_id} for user {current_user.id}")
        return db_contact
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Phone number already exists for another contact",
        )


@router.delete("/user/contacts/phone/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_phone_contact(
    contact_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete a phone contact."""
    db_contact = (
        db.query(UserPhoneContact)
        .filter(
            UserPhoneContact.id == contact_id,
            UserPhoneContact.owner_id == current_user.id,
        )
        .first()
    )

    if not db_contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Phone contact {contact_id} not found",
        )

    db.delete(db_contact)
    db.commit()
    logger.info(f"Deleted phone contact {contact_id} for user {current_user.id}")
