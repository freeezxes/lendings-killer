# Google OAuth 2.0 Implementation Prompt

## Project Context

**Project**: lendings-killer (FastAPI + Jinja2 + SQLite SaaS)  
**Purpose**: AI website builder for small businesses  
**Current Auth**: Phone + bcrypt password with DB-backed sessions  
**Database**: SQLite with users, sessions, sites, token_log, payments tables  

---

## Goal

Implement **"Continue with Google"** authentication as a secondary login/registration method alongside existing phone + password auth.

### Key Principle
**Do NOT break existing auth.** Preserve all phone/password functionality. Google OAuth is an *alternative* entry point only.

---

## User Stories

### Story 1: New User via Google
1. User clicks "Continue with Google" button
2. Redirected to Google OAuth consent screen
3. User grants permission
4. Google returns verified email + profile
5. New user created in DB: `email`, `google_id`, `auth_provider='google'`, 0 tokens
6. Session created, httponly cookie set
7. Redirected to `/payment?reason=welcome` (same as phone new users)
8. User buys first slot/credits, then creates site

### Story 2: Repeat Google Login
1. User clicks "Continue with Google"
2. Google OAuth flow completes
3. User found by `google_id`
4. Session created for existing user
5. Redirected to `/dashboard` (or `/payment` if 0 tokens)

### Story 3: Phone User Adds Google Later
1. Existing user registered with phone `+77064177628`, password, name
2. User logs out, clicks "Continue with Google"
3. Email from Google account is `john@gmail.com`
4. System checks: does email exist in DB? No
5. System checks: does phone exist in DB? Yes (but only if user provided email previously or we matched somehow)
6. **Decision A (Recommended)**: Create new separate Google user account (`john@gmail.com`)
   - User now has 2 accounts (phone-based and Google-based)
   - No linking required, simpler logic
7. **Decision B (Advanced)**: If email matches stored email, link google_id to existing user
   - Requires email field in existing phone users (optional data collection)
   - Complex matching logic

**We will use Decision A: No implicit account linking.** If user wants to link later, they can do so manually via account settings (future feature).

### Story 4: Login Cancellation
1. User clicks "Continue with Google"
2. User cancels consent screen
3. Redirected back to `/auth?error=user_cancelled` with clear message

---

## Database Changes

### Migration Strategy
Safe ALTER TABLE operations. **Do NOT drop or recreate tables.**

### New Columns (Add if not exist)
```python
# users table additions:
- email TEXT UNIQUE (nullable, indexed for lookups)
- google_id TEXT UNIQUE (nullable, stores Google sub claim)
- auth_provider TEXT DEFAULT 'local' (values: 'local', 'google')
- avatar_url TEXT (nullable, stores Google profile picture)
- password TEXT (make NULLABLE for Google-only users)
```

### Migration in db.py
```python
def migrate_add_oauth_columns():
    """Safe migration: add OAuth columns if they don't exist."""
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        
        if "email" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT UNIQUE")
        if "google_id" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN google_id TEXT UNIQUE")
        if "auth_provider" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT DEFAULT 'local'")
        if "avatar_url" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")
```

### No Existing Data Loss
- All existing users keep: `id`, `phone`, `password`, `name`, `tokens`, `site_slots`, `created`
- New columns are `NULL` for existing users
- `auth_provider` defaults to `'local'` for existing users (via DEFAULT clause or backfill)

---

## Environment Variables

### Required for Google OAuth
```bash
GOOGLE_CLIENT_ID=<from Google Cloud Console>
GOOGLE_CLIENT_SECRET=<from Google Cloud Console>
GOOGLE_REDIRECT_URI=http://127.0.0.1:8002/auth/google/callback  # local dev
```

### Production Example
```bash
GOOGLE_REDIRECT_URI=https://dum-e.com/auth/google/callback
```

### Local Dev Behavior
- If `GOOGLE_CLIENT_ID` is missing: Google button hidden from UI (or shows "Not configured" tooltip)
- No errors thrown if env vars missing—graceful degradation
- App still runs with phone-only auth

---

## Routes to Implement

### 1. GET /auth/google
**Purpose**: Initiate Google OAuth 2.0 flow

**Logic**:
1. Generate cryptographically random `state` token (32 bytes, base64)
2. Validate that Google env vars are configured
3. Store `state` in a **signed, encrypted cookie** with 10-minute TTL
4. Build Google OAuth URL with client_id, redirect_uri, scope, state
5. Redirect to Google's authorization endpoint

**Scopes**: `openid email profile`

**Pseudocode**:
```python
@app.get("/auth/google")
async def auth_google(request: Request):
    if not GOOGLE_CLIENT_ID:
        return RedirectResponse("/auth?error=google_not_configured")
    
    state = secrets.token_urlsafe(32)
    
    # Store state in signed cookie (10 min validity)
    response = RedirectResponse(url=google_auth_url)
    response.set_cookie(
        "oauth_state",
        state,
        httponly=True,
        secure=True,  # Only HTTPS in production
        samesite="lax",
        max_age=600  # 10 minutes
    )
    return response
```

### 2. GET /auth/google/callback
**Purpose**: Handle Google OAuth callback, verify token, create/login user

**Input**: 
- Query params: `code`, `state`

**Logic**:
1. Validate `state` cookie matches query param → CSRF protection
2. Exchange `code` for ID token (via Google token endpoint)
3. Verify ID token: signature, expiry, audience (client_id), issuer
4. Extract claims: `sub` (google_id), `email`, `email_verified`, `name`, `picture`
5. Validate: email must exist and be verified
6. Lookup user by `email` → if not found, create new user with `auth_provider='google'`
7. Create session, set httponly cookie
8. Redirect to appropriate destination:
   - If new user with 0 tokens: `/payment?reason=welcome`
   - If existing user with tokens: `/dashboard`
   - If existing user with 0 tokens: `/payment?reason=no_credits`

**Security**:
- Verify ID token signature using Google's public keys (via google-auth library)
- Check `email_verified=true` (reject unverified emails)
- Check token expiry
- Check `aud` (audience) matches client_id
- Verify `iss` (issuer) is `https://accounts.google.com` or `https://accounts.google.com/o/oauth2/v2/auth`

**Error Cases**:
- Missing `code` → `/auth?error=invalid_code`
- Invalid `state` → `/auth?error=invalid_state` + security log
- Google returns no email → `/auth?error=google_no_email`
- Email not verified → `/auth?error=email_not_verified`
- Token verification fails → `/auth?error=oauth_failed`
- DB error on user creation → `/auth?error=user_creation_failed` + log error

**Pseudocode**:
```python
@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str = None, state: str = None):
    # Validate state
    stored_state = request.cookies.get("oauth_state")
    if not state or not stored_state or state != stored_state:
        return RedirectResponse("/auth?error=invalid_state")
    
    # Exchange code for token (via HTTPS POST to Google)
    token = exchange_code_for_token(code)
    
    # Verify and decode ID token
    claims = verify_id_token(token["id_token"])
    
    # Validate claims
    if not claims.get("email"):
        return RedirectResponse("/auth?error=google_no_email")
    if not claims.get("email_verified"):
        return RedirectResponse("/auth?error=email_not_verified")
    
    # Lookup or create user
    email = claims["email"].lower()
    user = db.get_user_by_email(email)
    
    if not user:
        # Create new user
        user = db.create_google_user(
            email=email,
            google_id=claims["sub"],
            name=claims.get("name", ""),
            avatar_url=claims.get("picture", "")
        )
        dest = "/payment?reason=welcome"
    else:
        # Existing user, check tokens
        dest = "/dashboard" if user["tokens"] > 0 else "/payment?reason=no_credits"
    
    # Create session
    sid = db.create_session(user["id"])
    response = RedirectResponse(dest, status_code=302)
    response.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=365*24*3600)
    response.delete_cookie("oauth_state")  # Clean up state cookie
    return response
```

---

## Database Functions

### New functions in db.py

#### 1. get_user_by_email(email: str) → dict | None
```python
def get_user_by_email(email: str) -> dict | None:
    """Lookup user by normalized email (case-insensitive)."""
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE LOWER(email)=LOWER(?)", (email,)).fetchone()
        return dict(row) if row else None
```

#### 2. get_user_by_google_id(google_id: str) → dict | None
```python
def get_user_by_google_id(google_id: str) -> dict | None:
    """Lookup user by Google sub claim."""
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
        return dict(row) if row else None
```

#### 3. create_google_user(email: str, google_id: str, name: str, avatar_url: str) → dict | None
```python
def create_google_user(email: str, google_id: str, name: str = "", avatar_url: str = "") -> dict | None:
    """Create new user with Google OAuth. No password required."""
    try:
        with get_conn() as c:
            cur = c.execute(
                """INSERT INTO users 
                   (email, google_id, name, avatar_url, auth_provider, tokens) 
                   VALUES (?, ?, ?, ?, 'google', 0)""",
                (email, google_id, name, avatar_url)
            )
            return get_user_by_id(cur.lastrowid)
    except sqlite3.IntegrityError:
        # Email or google_id already exists
        return None
```

#### 4. link_google_to_user(user_id: int, google_id: str, avatar_url: str) → bool
```python
def link_google_to_user(user_id: int, google_id: str, avatar_url: str = "") -> bool:
    """Link Google account to existing local user (future feature)."""
    try:
        with get_conn() as c:
            c.execute(
                "UPDATE users SET google_id=?, avatar_url=? WHERE id=?",
                (google_id, avatar_url, user_id)
            )
        return True
    except sqlite3.IntegrityError:
        # google_id already linked to another user
        return False
```

---

## Template Changes

### Update templates/auth.html

**Location**: Add after the password login button, before the footer

**New HTML Section**:
```html
<!-- Google OAuth Button (show only if GOOGLE_CLIENT_ID is configured) -->
{% if google_configured %}
<a href="/auth/google" class="btn-google">
  <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
    <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
    <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
    <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
    <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
  </svg>
  <span>Continue with Google</span>
</a>
{% endif %}
```

### Add CSS for Google Button
```css
/* Google OAuth Button */
.btn-google {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
  padding: 13px;
  background: #1f2937;
  border: 1px solid var(--border);
  border-radius: 12px;
  color: var(--text);
  text-decoration: none;
  font-family: inherit;
  font-size: 0.95rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  margin-top: 12px;
}

.btn-google:hover {
  background: #374151;
  border-color: var(--accent);
}

.btn-google:active {
  transform: translateY(0);
  opacity: 1;
}

.btn-google svg {
  width: 18px;
  height: 18px;
}
```

### Pass google_configured to Template
```python
@app.get("/auth")
async def auth_page(request: Request):
    if request.state.user:
        return RedirectResponse("/create", status_code=302)
    
    google_configured = bool(os.getenv("GOOGLE_CLIENT_ID"))
    return templates.TemplateResponse(
        request,
        "auth.html",
        {"google_configured": google_configured}
    )
```

---

## Implementation Details

### Library Choice
- **google-auth** (2.25.2+) — ID token verification
- **httpx** — already used in project, use for token exchange
- **pydantic** — validate Google response (optional but recommended)

### Installation
```bash
pip install google-auth==2.25.2
```

### Code Organization
Create new file: `auth_google.py` (or add to existing structure)

**Structure**:
```python
# auth_google.py

import os
import secrets
import httpx
from google.auth.transport import requests
from google.oauth2 import id_token
import db

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8002/auth/google/callback")

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

async def get_google_config():
    """Fetch Google OIDC configuration."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(GOOGLE_DISCOVERY_URL)
        return resp.json()

def build_google_auth_url(state: str) -> str:
    """Build OAuth authorization URL."""
    config = {
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state
    }
    # Build URL...

async def exchange_code_for_token(code: str) -> dict:
    """Exchange authorization code for ID token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code"
            }
        )
        return resp.json()

def verify_id_token(id_token_str: str) -> dict:
    """Verify Google ID token signature and claims."""
    req = requests.Request()
    claims = id_token.verify_oauth2_token(
        id_token_str,
        req,
        clock_skew_in_seconds=10  # Small clock skew tolerance
    )
    
    # Verify issuer
    if claims["iss"] not in ["https://accounts.google.com", "https://accounts.google.com/o/oauth2/v2/auth"]:
        raise ValueError("Wrong issuer")
    
    # Verify audience
    if claims["aud"] != GOOGLE_CLIENT_ID:
        raise ValueError("Wrong audience")
    
    return claims
```

---

## Logout Compatibility

### Existing Logout Route
The current `/auth/logout` endpoint works for both phone and Google users:

```python
@app.post("/auth/logout")
async def auth_logout(request: Request):
    sid = request.cookies.get("sid")
    if sid:
        db.delete_session(sid)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("sid")
    return response
```

**No changes needed.** Logout doesn't care about auth provider—just deletes session.

---

## Security Checklist

- [ ] State parameter validated (CSRF protection)
- [ ] ID token signature verified via google-auth
- [ ] Token expiry checked
- [ ] Email verified flag checked (email_verified=true)
- [ ] Audience (aud) matches client_id
- [ ] Issuer (iss) is Google's domain
- [ ] HttpOnly cookies for sessions
- [ ] Secure cookies in production (secure=True when HTTPS)
- [ ] SameSite=lax on session cookies
- [ ] Client secret never sent to frontend
- [ ] State cookie deleted after verification
- [ ] No sensitive data logged
- [ ] Email normalized to lowercase for uniqueness
- [ ] Rate limiting on /auth/google/callback (optional, depends on deployment)

---

## Testing Checklist

After implementation:

- [ ] App starts: `python -m uvicorn main:app --reload --port 8002`
- [ ] Phone registration still works
- [ ] Phone login still works
- [ ] Google button appears on `/auth` (if GOOGLE_CLIENT_ID set)
- [ ] Google button hidden on `/auth` (if GOOGLE_CLIENT_ID missing)
- [ ] Can complete Google OAuth flow
- [ ] New Google user created with email, google_id, auth_provider='google'
- [ ] New Google user gets 0 tokens (redirected to /payment)
- [ ] Same Google user can log in again (no duplicate user)
- [ ] Session created for Google users
- [ ] Cookies are httponly
- [ ] Logout works for Google users
- [ ] `/dashboard` works after Google login
- [ ] `/admin` still works (verify user auth)
- [ ] Error handling: missing GOOGLE_CLIENT_ID shows clear error
- [ ] Error handling: cancelled Google login redirects to /auth
- [ ] No secrets printed in logs
- [ ] Existing phone users unaffected

---

## README Updates

Add to README.md:

### Google OAuth Setup

#### 1. Google Cloud Console Setup
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create new project or select existing
3. Enable **Google+ API**
4. Create **OAuth 2.0 Client ID** (Web Application):
   - Authorized JavaScript origins: `http://127.0.0.1:8002`, `https://dum-e.com`
   - Authorized redirect URIs:
     - Local: `http://127.0.0.1:8002/auth/google/callback`
     - Prod: `https://dum-e.com/auth/google/callback`
5. Copy **Client ID** and **Client Secret**

#### 2. Environment Variables

Create `.env` or export:
```bash
export GOOGLE_CLIENT_ID=<your_client_id>
export GOOGLE_CLIENT_SECRET=<your_client_secret>
export GOOGLE_REDIRECT_URI=http://127.0.0.1:8002/auth/google/callback  # local
# For production:
export GOOGLE_REDIRECT_URI=https://dum-e.com/auth/google/callback
```

#### 3. Local Testing

```bash
source venv/bin/activate
# Ensure env vars are set
python -m uvicorn main:app --reload --port 8002
```

Visit `http://127.0.0.1:8002/auth` and you should see "Continue with Google" button.

#### 4. Production Deployment

Update `.env` or deployment configuration with production redirect URI and client credentials.

---

## Rollout Strategy

### Phase 1: Local Dev
1. Add Google env vars locally
2. Test OAuth flow end-to-end
3. Verify existing phone auth still works
4. Check all error cases

### Phase 2: Staging
1. Deploy to staging server
2. Run full testing checklist
3. Monitor logs for errors
4. Verify cookie security

### Phase 3: Production
1. Update Google Cloud with production redirect URI
2. Deploy to prod
3. Monitor first 48 hours
4. Verify no user data corruption

---

## Edge Cases & Error Handling

| Scenario | Action |
|----------|--------|
| User cancels Google consent | Redirect to `/auth?error=user_cancelled` |
| Google returns no email | Redirect to `/auth?error=google_no_email` |
| Email not verified | Redirect to `/auth?error=email_not_verified` |
| Invalid OAuth state | Redirect to `/auth?error=invalid_state` + security log |
| Token verification fails | Redirect to `/auth?error=oauth_failed` + log |
| GOOGLE_CLIENT_ID not set | Hide Google button, app still works |
| Network error during token exchange | Redirect to `/auth?error=oauth_service_error` + log |
| Email already exists (race condition) | User logs in (get_user_by_email succeeds) |
| Google_id already linked to another user | Log error, redirect to `/auth?error=account_conflict` |

---

## Implementation Order

1. **Step 1**: Add migration function in `db.py`
2. **Step 2**: Add new DB query functions in `db.py`
3. **Step 3**: Create `auth_google.py` (or add to main.py)
4. **Step 4**: Add `/auth/google` and `/auth/google/callback` routes in `main.py`
5. **Step 5**: Update `templates/auth.html` with Google button
6. **Step 6**: Run migration, test end-to-end
7. **Step 7**: Update README.md with setup instructions

---

## Success Criteria

✅ Existing phone/password auth works unchanged  
✅ Google OAuth works end-to-end  
✅ New Google user created, gets 0 tokens, redirected to payment  
✅ Repeat Google login works (no duplicates)  
✅ Google button hidden if env vars missing  
✅ All error cases handled gracefully  
✅ No secrets in logs  
✅ Cookies are httponly and secure  
✅ Existing users unaffected  
✅ README updated with setup steps  

