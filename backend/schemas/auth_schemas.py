"""
auth_schemas.py — Pydantic Schemas for Authentication & User Management
========================================================================
Defines strict input and response models for:
- User registration
- User login & session profile
- Password reset and change
- OAuth discovery status
- WebSocket tickets
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, EmailStr, field_validator


class UserRegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100, description="User's display or full name")
    email: str = Field(..., description="Unique email address")
    password: str = Field(..., min_length=8, max_length=128, description="Strong account password")
    confirm_password: str = Field(..., description="Password confirmation")
    terms_accepted: bool = Field(default=True, description="Acceptance of terms of service and privacy policy")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        clean = v.strip().lower()
        if not clean or "@" not in clean or "." not in clean:
            raise ValueError("A valid email address is required.")
        return clean

    @field_validator("full_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        clean = v.strip()
        if len(clean) < 2:
            raise ValueError("Full name must be at least 2 characters.")
        return clean


class UserLoginRequest(BaseModel):
    email: str = Field(..., description="User's email address")
    password: str = Field(..., description="User's password")
    remember_me: bool = Field(default=False, description="Persist session for longer duration")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class UserProfileResponse(BaseModel):
    id: str
    full_name: str
    email: str
    is_active: bool
    is_verified: bool
    created_at: str
    last_login_at: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., description="Email address associated with the account")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=16, description="Opaque password reset token")
    new_password: str = Field(..., min_length=8, max_length=128, description="New password")
    confirm_password: str = Field(..., description="Confirm new password")


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., description="Existing active password")
    new_password: str = Field(..., min_length=8, max_length=128, description="New password")
    confirm_password: str = Field(..., description="Confirm new password")


class GenericAuthResponse(BaseModel):
    status: str = "ok"
    message: str


class OAuthStatusResponse(BaseModel):
    google_enabled: bool
    facebook_enabled: bool


class WebSocketTicketResponse(BaseModel):
    ticket: str
    session_id: str
    expires_in_seconds: int = 60


class OAuthAuthUrlResponse(BaseModel):
    authorization_url: str
    provider: str


class AuthMethodItem(BaseModel):
    provider: str
    email: Optional[str] = None
    linked_at: Optional[str] = None


class AuthMethodsResponse(BaseModel):
    user_id: str
    auth_methods: list[AuthMethodItem]


class UserProfileUpdateRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=100)
    profile_picture: Optional[str] = Field(None, max_length=1000)
    preferences: Optional[dict] = None


class MongoUserProfileResponse(BaseModel):
    user_id: str
    email: str
    full_name: str
    profile_picture: Optional[str] = None
    account_status: str
    auth_providers: list[str]
    preferences: dict
    created_at: str
    updated_at: str
    last_login_at: Optional[str] = None
    profile_sync_version: int = 1


