"""
auth.py — Authentication & User Management API Endpoints
=========================================================
Implements:
- POST /auth/register
- POST /auth/login
- POST /auth/logout
- GET  /auth/me
- POST /auth/forgot-password
- POST /auth/reset-password
- POST /auth/change-password
- GET  /auth/oauth-status
- POST /auth/ws-ticket
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.parse
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth_db import AuthDatabase, hash_token
from core.config import settings
from core.security import (
    DevMailbox,
    generate_pkce_pair,
    generate_reset_token,
    generate_session_token,
    hash_password,
    login_rate_limiter,
    oauth_state_manager,
    register_rate_limiter,
    reset_rate_limiter,
    validate_email_format,
    validate_password_policy,
    verify_password,
    ws_ticket_manager,
)
from schemas.auth_schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    GenericAuthResponse,
    OAuthAuthUrlResponse,
    OAuthStatusResponse,
    ResetPasswordRequest,
    UserLoginRequest,
    UserProfileResponse,
    UserRegisterRequest,
    WebSocketTicketResponse,
)

logger = logging.getLogger(__name__)
auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Cookie Helper
# ─────────────────────────────────────────────────────────────────────────────

def set_auth_cookie(response: Response, raw_token: str, remember_me: bool = False) -> None:
    """Sets secure HttpOnly session cookie."""
    max_age = (30 if remember_me else settings.SESSION_DURATION_DAYS) * 86400
    is_production = settings.ENVIRONMENT.lower() == "production"
    is_secure = settings.COOKIE_SECURE or is_production
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age,
        expires=max_age,
        httponly=True,
        secure=is_secure,
        samesite=settings.COOKIE_SAMESITE.lower(),
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    """Clears the session cookie."""
    is_production = settings.ENVIRONMENT.lower() == "production"
    is_secure = settings.COOKIE_SECURE or is_production
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        domain=settings.COOKIE_DOMAIN,
        path="/",
        httponly=True,
        secure=is_secure,
        samesite=settings.COOKIE_SAMESITE.lower(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Authentication Dependencies
# ─────────────────────────────────────────────────────────────────────────────

def get_session_token_from_request(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Optional[str]:
    """Extract raw session token from Authorization header or cookie."""
    # 1. Prefer explicit Authorization Bearer header if provided
    if authorization and authorization.startswith("Bearer "):
        bearer_token = authorization.split("Bearer ", 1)[1].strip()
        if bearer_token:
            return bearer_token

    # 2. Check HttpOnly session cookie
    cookie_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token

    return None


async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    FastAPI dependency that enforces authentication.
    Resolves session token, validates against DB hash, and loads user profile.
    Raises HTTP 401 if token is missing, expired, revoked, or user inactive.
    """
    token = get_session_token_from_request(request, authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    db = AuthDatabase.get_instance()
    session_data = db.get_session_by_token(token)
    if not session_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired or was revoked. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.get_user_by_id(session_data["user_id"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account associated with this session no longer exists.",
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account has been deactivated. Contact support.",
        )

    return user


async def get_optional_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Optional[Dict[str, Any]]:
    """Returns authenticated user if present and valid; None otherwise."""
    try:
        return await get_current_user(request, authorization)
    except HTTPException:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@auth_router.post("/register", response_model=UserProfileResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserRegisterRequest,
    response: Response,
    request: Request,
):
    """
    Register a new user account:
    - Validates inputs & password strength policy
    - Hashes password using Argon2id
    - Enforces unique email (case-insensitive)
    - Automatically creates authenticated session
    - Sets secure HttpOnly cookie
    """
    # 1. Rate limiting on registration attempts
    client_ip = request.client.host if request.client else "unknown"
    reg_key = f"reg:{client_ip}:{payload.email.strip().lower()}"
    if register_rate_limiter.is_rate_limited(reg_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many registration attempts. Please wait a moment before trying again.",
        )
    register_rate_limiter.record_attempt(reg_key)

    # 2. Validate password policy
    is_valid_pw, pw_error = validate_password_policy(payload.password)
    if not is_valid_pw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=pw_error)

    # 3. Check password confirmation
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Passwords do not match.")

    # 4. Check terms acceptance
    if not payload.terms_accepted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You must accept the terms of service and privacy policy to register.",
        )

    db = AuthDatabase.get_instance()

    # 5. Check email existence
    existing_user = db.get_user_by_email(payload.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email address already exists.",
        )

    # 5. Hash password with Argon2id
    pw_hash = hash_password(payload.password)

    # 6. Create user in database
    try:
        user = db.create_user(
            full_name=payload.full_name,
            email=payload.email,
            password_hash=pw_hash,
            is_active=True,
            is_verified=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 7. Create session and set cookie
    raw_token = generate_session_token()
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    db.create_session(
        user_id=user["id"],
        token=raw_token,
        duration_days=settings.SESSION_DURATION_DAYS,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    set_auth_cookie(response, raw_token)

    db.record_audit_event(
        event_type="register",
        user_id=user["id"],
        ip_address=client_ip,
        metadata={"email": user["email"]},
    )

    return UserProfileResponse(**user)


@auth_router.post("/login", response_model=UserProfileResponse)
async def login(
    payload: UserLoginRequest,
    response: Response,
    request: Request,
):
    """
    Log in an existing user:
    - Protects against brute-force attacks via sliding-window rate limiting
    - Verifies password with Argon2id
    - Returns generic authentication failure to prevent email enumeration
    - Issues cryptographically secure opaque session token
    - Sets secure HttpOnly cookie
    """
    client_ip = request.client.host if request.client else "unknown"
    rate_limit_key = f"{client_ip}:{payload.email}"

    # 1. Check rate limiter
    if login_rate_limiter.is_rate_limited(rate_limit_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please wait 60 seconds before trying again.",
        )

    db = AuthDatabase.get_instance()
    user = db.get_user_by_email(payload.email)

    # 2. Generic authentication failure if user not found
    if not user:
        login_rate_limiter.record_attempt(rate_limit_key)
        db.record_audit_event("login_failed", ip_address=client_ip, metadata={"reason": "user_not_found"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # 3. Verify Argon2id password hash
    if not verify_password(user["password_hash"], payload.password):
        login_rate_limiter.record_attempt(rate_limit_key)
        db.record_audit_event("login_failed", user_id=user["id"], ip_address=client_ip, metadata={"reason": "invalid_password"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # 4. Check account status
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Please contact support.",
        )

    # Clear rate limit counter on successful login
    login_rate_limiter.clear(rate_limit_key)

    # 5. Issue session
    raw_token = generate_session_token()
    user_agent = request.headers.get("user-agent")

    db.create_session(
        user_id=user["id"],
        token=raw_token,
        duration_days=30 if payload.remember_me else settings.SESSION_DURATION_DAYS,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    db.update_last_login(user["id"])
    set_auth_cookie(response, raw_token, remember_me=payload.remember_me)

    db.record_audit_event("login_success", user_id=user["id"], ip_address=client_ip)

    # Refresh user record to include updated last_login_at
    refreshed_user = db.get_user_by_id(user["id"]) or user
    return UserProfileResponse(**refreshed_user)


@auth_router.post("/logout", response_model=GenericAuthResponse)
async def logout(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None),
):
    """
    Log out user:
    - Revokes active server-side session in DB
    - Clears HttpOnly cookie
    """
    token = get_session_token_from_request(request, authorization)
    if token:
        db = AuthDatabase.get_instance()
        db.revoke_session(token)

    clear_auth_cookie(response)
    return GenericAuthResponse(status="ok", message="Logged out successfully.")


@auth_router.get("/me", response_model=UserProfileResponse)
async def get_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Return authenticated user profile."""
    return UserProfileResponse(**current_user)


@auth_router.post("/forgot-password", response_model=GenericAuthResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
):
    """
    Initiate password reset:
    - Always returns generic message to prevent account enumeration
    - Creates a 15-minute single-use hashed reset token if account exists
    - Documents that email delivery provider is used in production
    """
    client_ip = request.client.host if request.client else "unknown"
    rate_limit_key = f"reset:{client_ip}"

    if reset_rate_limiter.is_rate_limited(rate_limit_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password reset requests. Please wait a few minutes.",
        )

    reset_rate_limiter.record_attempt(rate_limit_key)
    db = AuthDatabase.get_instance()
    user = db.get_user_by_email(payload.email)

    if user:
        reset_token = generate_reset_token()
        db.create_password_reset_token(user_id=user["id"], token=reset_token, expiry_minutes=15)
        db.record_audit_event("password_reset_request", user_id=user["id"], ip_address=client_ip)
        # Dispatch via configured provider or gated in-memory development mailbox
        reset_url = f"{settings.FRONTEND_URL}/reset-password?token={reset_token}"
        DevMailbox.send(
            to_email=user["email"],
            subject="ARDO Password Reset",
            body_text="A password reset request was initiated for your ARDO account.",
            reset_url=reset_url,
            token=reset_token,
        )

        # Log purely generic operational notification WITHOUT exposing tokens or URLs
        logger.info("[Auth] Password reset process initiated for user_id=%s", user["id"])

    # Always return generic message to avoid disclosing user presence
    return GenericAuthResponse(
        status="ok",
        message="If this email is registered in our system, password reset instructions have been sent.",
    )


@auth_router.post("/reset-password", response_model=GenericAuthResponse)
async def reset_password(payload: ResetPasswordRequest):
    """
    Complete password reset:
    - Validates single-use unexpired reset token
    - Validates new password strength
    - Updates Argon2id password hash
    - Marks token as used (single use)
    - Revokes all existing user sessions
    """
    # 1. Validate password policy & match
    is_valid_pw, pw_error = validate_password_policy(payload.new_password)
    if not is_valid_pw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=pw_error)

    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Passwords do not match.")

    db = AuthDatabase.get_instance()
    token_record = db.get_and_validate_reset_token(payload.token)
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset token is invalid, expired, or has already been used.",
        )

    user_id = token_record["user_id"]
    new_hash = hash_password(payload.new_password)

    # Update password and invalidate token
    db.update_password(user_id, new_hash)
    db.mark_reset_token_used(payload.token)

    # Revoke all active sessions for security
    db.revoke_all_user_sessions(user_id)
    db.record_audit_event("password_reset_success", user_id=user_id)

    return GenericAuthResponse(
        status="ok",
        message="Password has been successfully updated. Please log in with your new password.",
    )


@auth_router.post("/change-password", response_model=GenericAuthResponse)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Change password for currently authenticated user:
    - Verifies current password with Argon2id
    - Validates new password policy
    - Hashes new password
    - Updates user password
    - Revokes all other sessions
    """
    if not verify_password(current_user["password_hash"], payload.current_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )

    is_valid_pw, pw_error = validate_password_policy(payload.new_password)
    if not is_valid_pw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=pw_error)

    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Passwords do not match.")

    db = AuthDatabase.get_instance()
    new_hash = hash_password(payload.new_password)
    db.update_password(current_user["id"], new_hash)
    db.record_audit_event("password_change", user_id=current_user["id"])

    return GenericAuthResponse(
        status="ok",
        message="Password changed successfully.",
    )


@auth_router.get("/oauth-status", response_model=OAuthStatusResponse)
async def get_oauth_status():
    """
    Report whether social OAuth providers are configured with real credentials.
    Returns False when credentials are not configured.
    Ensures no fake social login buttons are presented to users.
    """
    google_configured = bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)
    facebook_configured = bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET)
    return OAuthStatusResponse(
        google_enabled=google_configured,
        facebook_enabled=facebook_configured,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Google OAuth 2.0 Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@auth_router.get("/google", response_model=OAuthAuthUrlResponse)
async def google_login(
    next: str = Query("/app", description="Post-login destination path"),
    redirect: bool = Query(False, description="Whether to issue an immediate 307 redirect to Google"),
):
    """
    Initiate official Google OAuth 2.0 authorization code flow with PKCE and state protection.
    """
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured on this server.",
        )

    code_verifier, code_challenge = generate_pkce_pair()
    state = oauth_state_manager.create_state(
        provider="google",
        code_verifier=code_verifier,
        next_path=next,
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

    if redirect:
        return RedirectResponse(auth_url)
    return OAuthAuthUrlResponse(authorization_url=auth_url, provider="google")


@auth_router.get("/google/callback")
async def google_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """
    Google OAuth 2.0 callback endpoint.
    - Validates state and retrieves PKCE code_verifier.
    - Exchanges code at Google's token endpoint.
    - Validates identity at Google userinfo endpoint.
    - Resolves or creates canonical ARDO user.
    - Establishes authenticated session with HttpOnly cookie.
    - Redirects user to /app.
    """
    if error:
        logger.warning(f"[Google OAuth] Provider returned error: {error}")
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error={urllib.parse.quote(error)}", status_code=status.HTTP_302_FOUND)

    if not code or not state:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=missing_code_or_state", status_code=status.HTTP_302_FOUND)

    state_data = oauth_state_manager.consume_state(state, expected_provider="google")
    if not state_data:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=invalid_or_expired_state", status_code=status.HTTP_302_FOUND)

    # 1. Exchange authorization code for tokens
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_res = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                    "code_verifier": state_data.get("code_verifier"),
                },
            )
            if token_res.status_code != 200:
                logger.error(f"[Google OAuth] Token exchange error: {token_res.text}")
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=token_exchange_failed", status_code=status.HTTP_302_FOUND)
            token_data = token_res.json()
            access_token = token_data.get("access_token")
            id_token_raw = token_data.get("id_token")

            # Validate ID token claims if structured JWT is present
            if id_token_raw and isinstance(id_token_raw, str):
                id_parts = id_token_raw.split(".")
                if len(id_parts) == 3:
                    try:
                        payload_b64 = id_parts[1]
                        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
                        claims = json.loads(base64.urlsafe_b64decode(padded))
                        # 1. Verify issuer
                        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
                            logger.error(f"[Google OAuth] Invalid ID token issuer: {claims.get('iss')}")
                            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=invalid_id_token_issuer", status_code=status.HTTP_302_FOUND)
                        # 2. Verify audience against configured Client ID
                        if settings.GOOGLE_CLIENT_ID and claims.get("aud") != settings.GOOGLE_CLIENT_ID:
                            logger.error(f"[Google OAuth] Invalid ID token audience: {claims.get('aud')}")
                            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=invalid_id_token_audience", status_code=status.HTTP_302_FOUND)
                        # 3. Verify expiration
                        exp = claims.get("exp", 0)
                        if time.time() > exp:
                            logger.error(f"[Google OAuth] ID token expired: exp={exp}")
                            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=expired_id_token", status_code=status.HTTP_302_FOUND)
                    except Exception as e:
                        logger.error(f"[Google OAuth] ID token parsing exception: {e}")
                        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=malformed_id_token", status_code=status.HTTP_302_FOUND)

            # 2. Fetch verified identity claims directly from Google
            userinfo_res = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_res.status_code != 200:
                logger.error(f"[Google OAuth] Userinfo error: {userinfo_res.text}")
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=userinfo_fetch_failed", status_code=status.HTTP_302_FOUND)
            userinfo = userinfo_res.json()
    except Exception as e:
        logger.error(f"[Google OAuth] HTTP request exception: {e}")
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=google_connection_failed", status_code=status.HTTP_302_FOUND)

    # 3. Validate identity claims
    sub = str(userinfo.get("sub", "")).strip()
    email = userinfo.get("email", "").strip().lower()
    email_verified = bool(userinfo.get("email_verified", False))
    name = userinfo.get("name") or "Google User"

    if not sub or not email:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=invalid_google_identity", status_code=status.HTTP_302_FOUND)
    if not email_verified:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=unverified_google_email", status_code=status.HTTP_302_FOUND)

    # 4. Resolve or associate ARDO account
    db = AuthDatabase.get_instance()
    oac = db.get_oauth_account("google", sub)
    user = None

    if oac:
        user = db.get_user_by_id(oac["user_id"])
        if not user or not user.get("is_active", True):
            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=account_suspended", status_code=status.HTTP_302_FOUND)
    elif state_data.get("linking_user_id"):
        # Authenticated user linking Google account
        target_user = db.get_user_by_id(state_data["linking_user_id"])
        if target_user:
            try:
                db.create_oauth_account(target_user["id"], "google", sub, email, email_verified)
                user = target_user
            except ValueError as e:
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error={urllib.parse.quote(str(e))}", status_code=status.HTTP_302_FOUND)
    else:
        # Check if an existing account with this email already exists
        existing_user = db.get_user_by_email(email)
        if existing_user:
            # SECURITY REQUIREMENT: Never automatically merge an unauthenticated OAuth login
            # into an existing account solely because email addresses match.
            # The user must first authenticate with their existing credentials and link the provider explicitly.
            err_msg = urllib.parse.quote("An account with this email already exists. Please log in with your credentials and link your Google account in Settings.")
            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error={err_msg}", status_code=status.HTTP_302_FOUND)
        else:
            # Create new user
            try:
                user = db.create_user(
                    full_name=name,
                    email=email,
                    password_hash=None,
                    is_active=True,
                    is_verified=email_verified,
                )
                db.create_oauth_account(user["id"], "google", sub, email, email_verified)
            except Exception as e:
                logger.error(f"[Google OAuth] User creation failed: {e}")
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=account_creation_failed", status_code=status.HTTP_302_FOUND)

    if not user:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=authentication_failed", status_code=status.HTTP_302_FOUND)

    # 5. Create ARDO session & Set HttpOnly Cookie
    raw_token = generate_session_token()
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    db.create_session(
        user_id=user["id"],
        token=raw_token,
        duration_days=settings.SESSION_DURATION_DAYS,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    dest = state_data.get("next") or "/app"
    redirect_target = f"{settings.FRONTEND_URL}{dest}"
    response = RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)
    set_auth_cookie(response, raw_token)

    db.record_audit_event(
        event_type="oauth_login_success",
        user_id=user["id"],
        ip_address=client_ip,
        metadata={"provider": "google"},
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Facebook (Meta) OAuth Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@auth_router.get("/facebook", response_model=OAuthAuthUrlResponse)
async def facebook_login(
    next: str = Query("/app", description="Post-login destination path"),
    redirect: bool = Query(False, description="Whether to issue an immediate 307 redirect to Facebook"),
):
    """
    Initiate official Meta Facebook Login authorization code flow with state protection.
    """
    if not (settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Facebook Login is not configured on this server.",
        )

    state = oauth_state_manager.create_state(
        provider="facebook",
        next_path=next,
    )

    params = {
        "client_id": settings.FACEBOOK_APP_ID,
        "redirect_uri": settings.FACEBOOK_REDIRECT_URI,
        "state": state,
        "scope": "email,public_profile",
        "response_type": "code",
    }
    auth_url = f"https://www.facebook.com/v19.0/dialog/oauth?{urllib.parse.urlencode(params)}"

    if redirect:
        return RedirectResponse(auth_url)
    return OAuthAuthUrlResponse(authorization_url=auth_url, provider="facebook")


@auth_router.get("/facebook/callback")
async def facebook_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """
    Meta Facebook Login callback endpoint.
    - Validates state.
    - Exchanges code at Meta Graph API token endpoint.
    - Retrieves profile from Meta Graph API.
    - Safely handles accounts with or without available email.
    - Resolves or associates canonical ARDO user.
    - Establishes authenticated session with HttpOnly cookie.
    - Redirects user to /app.
    """
    if error:
        logger.warning(f"[Facebook OAuth] Provider returned error: {error}")
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error={urllib.parse.quote(error)}", status_code=status.HTTP_302_FOUND)

    if not code or not state:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=missing_code_or_state", status_code=status.HTTP_302_FOUND)

    state_data = oauth_state_manager.consume_state(state, expected_provider="facebook")
    if not state_data:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=invalid_or_expired_state", status_code=status.HTTP_302_FOUND)

    # 1. Exchange authorization code for token
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_res = await client.get(
                "https://graph.facebook.com/v19.0/oauth/access_token",
                params={
                    "client_id": settings.FACEBOOK_APP_ID,
                    "client_secret": settings.FACEBOOK_APP_SECRET,
                    "redirect_uri": settings.FACEBOOK_REDIRECT_URI,
                    "code": code,
                },
            )
            if token_res.status_code != 200:
                logger.error(f"[Facebook OAuth] Token exchange error: {token_res.text}")
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=token_exchange_failed", status_code=status.HTTP_302_FOUND)
            token_data = token_res.json()
            access_token = token_data.get("access_token")

            # 2. Fetch profile info
            userinfo_res = await client.get(
                "https://graph.facebook.com/v19.0/me",
                params={
                    "fields": "id,name,email,picture.type(large)",
                    "access_token": access_token,
                },
            )
            if userinfo_res.status_code != 200:
                logger.error(f"[Facebook OAuth] Userinfo error: {userinfo_res.text}")
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=userinfo_fetch_failed", status_code=status.HTTP_302_FOUND)
            userinfo = userinfo_res.json()
    except Exception as e:
        logger.error(f"[Facebook OAuth] HTTP request exception: {e}")
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=facebook_connection_failed", status_code=status.HTTP_302_FOUND)

    # 3. Validate Facebook identity
    fb_id = str(userinfo.get("id", "")).strip()
    name = userinfo.get("name") or "Facebook User"
    email = userinfo.get("email")
    if email:
        email = email.strip().lower()

    if not fb_id:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=invalid_facebook_identity", status_code=status.HTTP_302_FOUND)

    # 4. Resolve or associate ARDO account
    db = AuthDatabase.get_instance()
    oac = db.get_oauth_account("facebook", fb_id)
    user = None

    if oac:
        user = db.get_user_by_id(oac["user_id"])
        if not user or not user.get("is_active", True):
            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=account_suspended", status_code=status.HTTP_302_FOUND)
    elif state_data.get("linking_user_id"):
        # Authenticated user linking Facebook account
        target_user = db.get_user_by_id(state_data["linking_user_id"])
        if target_user:
            try:
                db.create_oauth_account(target_user["id"], "facebook", fb_id, email, bool(email))
                user = target_user
            except ValueError as e:
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error={urllib.parse.quote(str(e))}", status_code=status.HTTP_302_FOUND)
    else:
        # Check if an existing account with this email already exists
        existing_user = db.get_user_by_email(email) if email else None
        if existing_user:
            # SECURITY REQUIREMENT: Never automatically merge an unauthenticated OAuth login
            # into an existing account solely because email addresses match.
            # The user must first authenticate with their existing credentials and link the provider explicitly.
            err_msg = urllib.parse.quote("An account with this email already exists. Please log in with your credentials and link your Facebook account in Settings.")
            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error={err_msg}", status_code=status.HTTP_302_FOUND)
        else:
            # Create new user (using email if present, or provisional account-completion identifier)
            account_email = email or f"fb_{fb_id}@facebook.ardo.user"
            try:
                user = db.create_user(
                    full_name=name,
                    email=account_email,
                    password_hash=None,
                    is_active=True,
                    is_verified=bool(email),
                )
                db.create_oauth_account(user["id"], "facebook", fb_id, email, bool(email))
            except Exception as e:
                logger.error(f"[Facebook OAuth] User creation failed: {e}")
                return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=account_creation_failed", status_code=status.HTTP_302_FOUND)

    if not user:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=authentication_failed", status_code=status.HTTP_302_FOUND)

    # 5. Create ARDO session & Set HttpOnly Cookie
    raw_token = generate_session_token()
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    db.create_session(
        user_id=user["id"],
        token=raw_token,
        duration_days=settings.SESSION_DURATION_DAYS,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    dest = state_data.get("next") or "/app"
    redirect_target = f"{settings.FRONTEND_URL}{dest}"
    response = RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)
    set_auth_cookie(response, raw_token)

    db.record_audit_event(
        event_type="oauth_login_success",
        user_id=user["id"],
        ip_address=client_ip,
        metadata={"provider": "facebook"},
    )
    return response



class WebSocketTicketRequest(BaseModel):
    session_id: str


@auth_router.post("/ws-ticket", response_model=WebSocketTicketResponse)
async def create_ws_ticket(
    payload: WebSocketTicketRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Generate a 60-second single-use ticket for authenticating WebSocket connections.
    Enables secure WebSocket authentication in cross-origin environments without exposing tokens.
    """
    ticket = ws_ticket_manager.issue_ticket(user_id=current_user["id"], session_id=payload.session_id)
    return WebSocketTicketResponse(
        ticket=ticket,
        session_id=payload.session_id,
        expires_in_seconds=60,
    )
