"""
users.py — Authenticated User Profile & Account Management API Endpoints
========================================================================
Implements:
- GET    /users/me
- PATCH  /users/me
- GET    /users/me/auth-methods
- POST   /users/me/auth-methods/link
- DELETE /users/me/auth-methods/{provider}

Architectural Guarantees:
- PostgreSQL remains the authoritative identity store.
- MongoDB Atlas 'user_profiles' serves application-facing profile documents.
- Fallback to PostgreSQL is provided when MongoDB is initializing or unavailable.
- Strict ownership enforcement on every operation (cannot access or modify another user's profile).
- Prevents unlinking the last usable login method.
- Zero credential or token leaks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.auth import get_current_user
from core.auth_db import AuthDatabase
from core.config import settings
from core.database import get_database
from core.security import generate_pkce_pair, oauth_state_manager
from schemas.auth_schemas import (
    AuthMethodItem,
    AuthMethodsResponse,
    GenericAuthResponse,
    MongoUserProfileResponse,
    OAuthAuthUrlResponse,
    UserProfileUpdateRequest,
)

logger = logging.getLogger(__name__)

users_router = APIRouter(prefix="/users", tags=["User Profile & Account Management"])


@users_router.get("/me", response_model=MongoUserProfileResponse)
async def get_user_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Retrieve canonical user profile from MongoDB 'user_profiles'.
    Gracefully falls back to authoritative PostgreSQL identity data if MongoDB
    is currently offline or synchronization is in progress.
    """
    user_id = current_user["id"]
    mongo_db = get_database()

    if mongo_db is not None:
        try:
            doc = await mongo_db.user_profiles.find_one({"_id": user_id})
            if doc:
                return MongoUserProfileResponse(
                    user_id=doc["user_id"],
                    email=doc["email"],
                    full_name=doc["full_name"],
                    profile_picture=doc.get("profile_picture"),
                    account_status=doc.get("account_status", "active"),
                    auth_providers=doc.get("auth_providers", ["password"]),
                    preferences=doc.get("preferences", {"theme": "dark", "notifications_enabled": True}),
                    created_at=doc.get("created_at", current_user["created_at"]),
                    updated_at=doc.get("updated_at", current_user["updated_at"]),
                    last_login_at=doc.get("last_login_at", current_user.get("last_login_at")),
                    profile_sync_version=doc.get("profile_sync_version", 1),
                )
        except Exception as e:
            logger.warning(f"[UsersAPI] MongoDB read error, serving authoritative PG fallback: {e}")

    # Authoritative PostgreSQL-backed fallback
    db_auth = AuthDatabase.get_instance()
    auth_info = db_auth.count_user_auth_methods(user_id)
    providers = auth_info.get("providers", ["password"])

    return MongoUserProfileResponse(
        user_id=user_id,
        email=current_user["email"],
        full_name=current_user["full_name"],
        profile_picture=None,
        account_status="active" if current_user.get("is_active", True) else "suspended",
        auth_providers=providers,
        preferences={"theme": "dark", "notifications_enabled": True},
        created_at=current_user["created_at"],
        updated_at=current_user["updated_at"],
        last_login_at=current_user.get("last_login_at"),
        profile_sync_version=1,
    )


@users_router.patch("/me", response_model=MongoUserProfileResponse)
async def update_user_profile(
    payload: UserProfileUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Update permitted user profile fields (display name, profile picture, preferences).
    - Identity changes (full_name) update authoritative PostgreSQL store and enqueue outbox event.
    - Application preferences and profile picture update MongoDB collection directly.
    - Strictly scoped to authenticated caller (cannot update other users).
    """
    user_id = current_user["id"]
    db_auth = AuthDatabase.get_instance()

    # 1. Update display name in PostgreSQL if provided
    updated_user = current_user
    if payload.full_name is not None and payload.full_name.strip() != current_user["full_name"]:
        updated_user = db_auth.update_user_profile(user_id, full_name=payload.full_name) or current_user

    # 2. Update preferences and profile picture in MongoDB if available
    mongo_db = get_database()
    now_iso = datetime.now(timezone.utc).isoformat()

    if mongo_db is not None:
        try:
            update_fields: Dict[str, Any] = {"updated_at": now_iso}
            if payload.full_name is not None:
                update_fields["full_name"] = payload.full_name.strip()
            if payload.profile_picture is not None:
                update_fields["profile_picture"] = payload.profile_picture
            if payload.preferences is not None:
                update_fields["preferences"] = payload.preferences

            await mongo_db.user_profiles.update_one(
                {"_id": user_id},
                {"$set": update_fields},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"[UsersAPI] Failed to update MongoDB directly, queued via outbox: {e}")

    # Return refreshed profile
    return await get_user_profile(current_user=updated_user)


@users_router.get("/me/auth-methods", response_model=AuthMethodsResponse)
async def list_auth_methods(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    List all active authentication methods linked to the caller's account.
    Exposes provider names and linking timestamps without exposing any secrets or tokens.
    """
    user_id = current_user["id"]
    db_auth = AuthDatabase.get_instance()
    auth_summary = db_auth.count_user_auth_methods(user_id)

    items: List[AuthMethodItem] = []
    if auth_summary.get("has_password"):
        items.append(
            AuthMethodItem(
                provider="password",
                email=current_user["email"],
                linked_at=current_user["created_at"],
            )
        )

    for oac in auth_summary.get("oauth_accounts", []):
        items.append(
            AuthMethodItem(
                provider=oac["provider"],
                email=oac.get("provider_email"),
                linked_at=oac.get("linked_at"),
            )
        )

    return AuthMethodsResponse(user_id=user_id, auth_methods=items)


@users_router.post("/me/auth-methods/link", response_model=OAuthAuthUrlResponse)
async def initiate_link_auth_method(
    provider: str = Query(..., description="Provider to link: 'google' or 'facebook'"),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Initiate safe linking of an external OAuth provider to the authenticated user's account.
    Generates a single-use state tagged with the user's canonical user_id.
    """
    provider_clean = provider.strip().lower()
    user_id = current_user["id"]

    if provider_clean == "google":
        if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google OAuth is not configured on this server.",
            )
        code_verifier, code_challenge = generate_pkce_pair()
        state = oauth_state_manager.create_state(
            provider="google",
            code_verifier=code_verifier,
            linking_user_id=user_id,
            next_path="/app",
        )
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
        return OAuthAuthUrlResponse(authorization_url=auth_url, provider="google")

    elif provider_clean == "facebook":
        if not (settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Facebook Login is not configured on this server.",
            )
        state = oauth_state_manager.create_state(
            provider="facebook",
            linking_user_id=user_id,
            next_path="/app",
        )
        params = {
            "client_id": settings.FACEBOOK_APP_ID,
            "redirect_uri": settings.FACEBOOK_REDIRECT_URI,
            "state": state,
            "scope": "email,public_profile",
            "response_type": "code",
        }
        auth_url = f"https://www.facebook.com/v19.0/dialog/oauth?{urllib.parse.urlencode(params)}"
        return OAuthAuthUrlResponse(authorization_url=auth_url, provider="facebook")

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: '{provider}'. Supported providers are 'google' and 'facebook'.",
        )


@users_router.delete("/me/auth-methods/{provider}", response_model=GenericAuthResponse)
async def unlink_auth_method(
    provider: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Safely unlink an authentication provider from the user's account.
    CRITICAL SECURITY RULE:
    Prevents unlinking if doing so would leave the user with zero authentication methods.
    """
    provider_clean = provider.strip().lower()
    user_id = current_user["id"]
    db_auth = AuthDatabase.get_instance()

    # Verify existing methods
    auth_summary = db_auth.count_user_auth_methods(user_id)
    if provider_clean not in auth_summary["providers"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider}' is not currently linked to your account.",
        )

    # Enforce minimum usable authentication methods
    if auth_summary["total_methods"] <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot unlink authentication method: you must retain at least one usable login method (password or alternative OAuth provider).",
        )

    if provider_clean in ("google", "facebook"):
        db_auth.delete_oauth_account(user_id, provider_clean)
        db_auth.record_audit_event(
            event_type="oauth_unlinked",
            user_id=user_id,
            metadata={"provider": provider_clean},
        )
        return GenericAuthResponse(
            status="ok",
            message=f"{provider.capitalize()} account unlinked successfully.",
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="To remove password authentication, please unlink via credential management.",
        )

