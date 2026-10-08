# PowerLead Response backend

FastAPI backend using SQLAlchemy 2.0, PostgreSQL, Alembic, Pydantic validation, bcrypt password hashing, and JWT bearer authentication.

## Installation

```powershell
cd F:\CODEX\Project\powerlead-response\BackEnd
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Database environment values are read from the existing `.env`. Connection selection remains centralized in `app/core/config.py`.

## Database selection

- `LIVE=false` selects `LOCAL_DATABASE_URL`.
- `LIVE=true` selects `PRODUCTION_DATABASE_URL`.

Restart Uvicorn after changing the selected environment.

The same selector controls the only allowed browser origin:

- `LIVE=false`: `http://localhost:4200`
- `LIVE=true`: `https://powerleadresponse.netlify.app`

Both frontend URLs are maintained in `app/core/config.py`.

## JWT configuration

Set these values in the local `.env` file. Keep the real secret out of source control:

```dotenv
JWT_SECRET_KEY=replace-with-a-cryptographically-random-secret-of-at-least-32-characters
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
PASSWORD_RESET_TOKEN_EXPIRE_MINUTES=15
PASSWORD_RESET_PATH=/reset-password
```

Generate a suitable development secret with Python:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use a separately managed secret in production. `JWT_ALGORITHM` is restricted to `HS256`, and `ACCESS_TOKEN_EXPIRE_MINUTES` must be greater than zero.

## Migrations

Apply all committed migrations:

```powershell
alembic upgrade head
```

The tenant authorization migration was generated and applied with:

```powershell
alembic revision --autogenerate -m "add login ids and tenant authorization"
alembic upgrade head
```

The current migration chain preserves an existing `users` table by renaming it to `t_user`, then adds `is_active`, the unique email constraint, the email index, and the `updated_at` trigger. It also creates `t_password_reset_token` with its foreign key and indexes.

The password-reset migration was generated with:

```powershell
alembic revision --autogenerate -m "create t_password_reset_token table"
alembic upgrade head
```

The tenant migration safely backfills existing accounts with `user-<full UUID>` login IDs. It seeds role and permission definitions but creates no tenant memberships and promotes no users.

The tenant settings administration migration was generated and reviewed with:

```powershell
alembic revision --autogenerate -m "add tenant settings administration"
alembic upgrade head
```

For an existing checkout, do not generate the migration again. Apply the committed migration with `alembic upgrade head`. It extends the existing role/permission system, preserves existing data, and seeds the developer-defined menu/action permissions for the system roles.

The nullable lead service field migration was generated and reviewed with:

```powershell
alembic revision --autogenerate -m "add service requested to leads"
alembic upgrade head
```

The committed migration adds only `t_lead.service_requested` as a nullable `varchar(200)` column. Existing lead records remain unchanged with a null value.

## Run

```powershell
uvicorn app.main:app --reload
```

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- Health check: http://127.0.0.1:8000/health

## Register a user

`POST /api/auth/register`

```json
{
  "name": "John Doe",
  "email": "john@gmail.com",
  "login_id": "john.doe",
  "password": "123456",
  "confirm_password": "123456"
}
```

Success (`201 Created`):

```json
{
  "success": true,
  "message": "Registration successful",
  "data": {
    "id": "user_uuid",
    "name": "John Doe",
    "email": "john@gmail.com",
    "login_id": "john.doe"
  }
}
```

Duplicate email (`400 Bad Request`):

```json
{
  "success": false,
  "message": "Email already registered"
}
```

Passwords and password hashes are never returned by the API.

`login_id` is optional during the frontend migration. If omitted, the backend generates a unique `user-<UUID>` value. Login IDs are normalized to lowercase and accept 5–50 letters, numbers, dots, underscores, or hyphens. Public registration rejects role fields and never creates a workspace membership.

## Log in

`POST /api/auth/login`

Request body:

```json
{
  "identifier": "john.doe",
  "password": "12345678"
}
```

`identifier` accepts either email or login ID. Existing clients may temporarily continue using the legacy request:

```json
{
  "email": "user@gmail.com",
  "password": "12345678"
}
```

Success (`200 OK`):

```json
{
  "success": true,
  "message": "Login successful",
  "data": {
    "access_token": "jwt_token",
    "token_type": "bearer",
    "auth_status": "workspace_selected",
    "user": {
      "id": "user_uuid",
      "login_id": "john.doe",
      "name": "John Doe",
      "email": "user@gmail.com"
    },
    "selected_workspace": {
      "id": "workspace_uuid",
      "name": "Demo Workspace",
      "slug": "demo-workspace"
    },
    "role": "owner",
    "permissions": ["tenant.users.manage", "leads.manage"],
    "workspaces": [
      {
        "id": "workspace_uuid",
        "name": "Demo Workspace",
        "slug": "demo-workspace",
        "role": "owner"
      }
    ]
  }
}
```

Invalid credentials (`401 Unauthorized`):

```json
{
  "success": false,
  "message": "Invalid identifier or password"
}
```

For protected endpoints, send the token in `Authorization: Bearer <access_token>`. The reusable bearer validation dependency is in `app/core/security.py`.

Login status behavior:

- One active membership: `workspace_selected`; the JWT is scoped to that membership.
- Multiple active memberships: `workspace_selection_required`; permissions remain empty until selection.
- No active membership: `onboarding_pending`; the user is authenticated but has no tenant permissions.

Passwords are passed unchanged to bcrypt verification. They are never trimmed or normalized.

## Select a workspace

`POST /api/auth/select-workspace`

```json
{
  "workspace_id": "workspace_uuid"
}
```

Send the login token as a bearer token. The response uses the same contract as login and returns a new workspace-scoped token. Unauthorized workspace selection returns `403`.

## Current authenticated session

`GET /api/auth/me`

```json
{
  "success": true,
  "data": {
    "auth_status": "workspace_selected",
    "user": {
      "id": "user_uuid",
      "login_id": "john.doe",
      "name": "John Doe",
      "email": "user@gmail.com"
    },
    "selected_workspace": {
      "id": "workspace_uuid",
      "name": "Demo Workspace",
      "slug": "demo-workspace"
    },
    "role": "owner",
    "permissions": ["tenant.users.manage", "leads.manage"],
    "workspaces": [
      {
        "id": "workspace_uuid",
        "name": "Demo Workspace",
        "slug": "demo-workspace",
        "role": "owner"
      }
    ]
  }
}
```

The API resolves membership, role, and permissions from the database on every protected request. Revoked memberships and role changes therefore take effect immediately, even for an existing JWT.

## Workspace authorization

Owner-only endpoints:

- `GET /api/workspaces/{workspace_id}/members`
- `PATCH /api/workspaces/{workspace_id}/members/{membership_id}/role`
- `DELETE /api/workspaces/{workspace_id}/members/{membership_id}`
- `POST /api/workspaces/{workspace_id}/members/{membership_id}/integration-access`

Role assignment request:

```json
{
  "role": "operator"
}
```

Role changes, revocations, and integration delegation are audited. The final active owner cannot be demoted or revoked. Workspace IDs are checked against the live token membership to prevent cross-tenant access.

Permission policy:

- `owner`: tenant users/settings, approvals, leads, audits, and integrations.
- `operator`: leads, drafts, workflows, and audits; no role management or approvals.
- `staff`: dashboard access and read/update access limited to assigned leads.
- `readonly`: read permissions only.
- Operator integration access is absent by default and requires the explicit delegation endpoint.

`app/core/security.py` provides reusable workspace permission and staff assigned-lead scope enforcement for future lead routes.

## Tenant settings administration

All settings endpoints require `Authorization: Bearer <workspace-scoped-access-token>`. The tenant is resolved from the verified membership in the token and database; clients do not send a tenant ID. List endpoints accept bounded `page` and `size` query parameters (`page >= 1`, `1 <= size <= 100`).

### Roles

- `GET /api/settings/roles?page=1&size=20`
- `POST /api/settings/roles`
- `PATCH /api/settings/roles/{role_id}`
- `DELETE /api/settings/roles/{role_id}`

Create or rename a custom role:

```json
{
  "name": "Sales Manager"
}
```

Successful create response (`201 Created`):

```json
{
  "success": true,
  "message": "Role created",
  "data": {
    "id": "role_uuid",
    "key": "custom-role-key",
    "name": "Sales Manager",
    "is_system": false
  }
}
```

Names are trimmed, 2-100 characters, and unique case-insensitively inside the workspace. `owner`, `operator`, `staff`, and `readonly` are system roles and cannot be renamed or deleted. Deleting a role that is or was assigned to a membership returns `409 Conflict` to preserve authorization history.

### User setup

- `GET /api/settings/users?page=1&size=20`
- `POST /api/settings/users`
- `PATCH /api/settings/users/{user_id}`
- `DELETE /api/settings/users/{user_id}`
- `POST /api/settings/users/{user_id}/reset-password`

Create a user and workspace membership atomically:

```json
{
  "display_name": "Jane Doe",
  "email": "jane@example.com",
  "login_id": "jane.doe",
  "password": "a-secure-password",
  "confirm_password": "a-secure-password",
  "role_id": "role_uuid"
}
```

Email is required because the global user model requires it. Existing global accounts are not silently linked by email or login ID. Login IDs use the established lowercase, globally unique 5-50 character policy. Responses never include passwords or password hashes.

Edit the membership-scoped name/role fields:

```json
{
  "display_name": "Jane Smith",
  "role_id": "role_uuid"
}
```

Reset a password through the dedicated audited action:

```json
{
  "password": "a-new-secure-password",
  "confirm_password": "a-new-secure-password"
}
```

Deleting a user setup revokes only the current workspace membership; it does not delete the global account or historical records. Global profile/password changes are rejected when the account belongs to multiple workspaces. The final active owner cannot be demoted or revoked.

### Menu permissions

- `GET /api/settings/menu-catalog`
- `GET /api/settings/roles/{role_id}/permissions`
- `PUT /api/settings/roles/{role_id}/permissions`

Replace a custom role's managed menu permissions:

```json
{
  "permission_keys": [
    "overview.view",
    "leads.view",
    "leads.update"
  ]
}
```

Unknown keys and invalid combinations are rejected. Every write/action permission requires the matching `.view` permission. Existing non-menu authorization mappings are preserved. New custom roles begin with no permissions.

Only an owner can assign the owner role or delegate privileged settings-management permissions. A delegated manager cannot modify a system role, edit their own role, grant permissions they do not possess, grant privileged management permissions, or otherwise escalate their authority.

Developer-defined permission keys:

- `overview.view`
- `leads.view`, `leads.create`, `leads.update`, `leads.delete`, `leads.export`
- `workflows.view`, `workflows.create`, `workflows.update`, `workflows.delete`, `workflows.approve`, `workflows.activate`
- `audits.view`, `audits.create`, `audits.update`, `audits.delete`, `audits.export`
- `templates.view`, `templates.create`, `templates.update`, `templates.delete`, `templates.approve`
- `settings.roles.view`, `settings.roles.create`, `settings.roles.update`, `settings.roles.delete`
- `settings.users.view`, `settings.users.create`, `settings.users.update`, `settings.users.delete`, `settings.users.reset_password`
- `settings.menu_permissions.view`, `settings.menu_permissions.update`
- `settings.integrations.view`, `settings.integrations.update`

The settings router enforces the corresponding `settings.*` permission on every endpoint. `/api/auth/me` resolves and returns the current effective role and permission keys from the database, so role changes, permission removals, and membership revocation take effect on the next protected request.

## Local workspace bootstrap

No existing user is promoted automatically. After applying migrations, explicitly create or reuse a local workspace and assign an existing account as Owner with:

```powershell
python -m app.commands.bootstrap_workspace `
  --identifier "<EXACT_LOGIN_ID_OR_EMAIL>" `
  --workspace-name "<WORKSPACE_NAME>"
```

The identifier must match an existing user's email or login ID; display names are never matched. The slug is derived from the workspace name. To choose it explicitly, append `--workspace-slug "my-workspace"`.

The command is transactional, safe to rerun, and disabled when `LIVE=true`. It does not create a user, alter a password, or print a password/token. It creates or reuses the workspace, reactivates/reuses the membership, assigns the existing system Owner role, and repairs any missing Owner mappings for Role, User Setup, and Menu Permission. The Owner can then log in again to receive a workspace-scoped token. `/api/auth/me` returns the selected workspace, `owner` role, and current effective permission keys.

## Lead management

All lead endpoints require a workspace-scoped bearer token. Tenant and audit fields are always derived server-side.

### CRUD endpoints

- `POST /api/leads` — `leads.create`
- `GET /api/leads` — `leads.view`
- `GET /api/leads/{lead_id}` — `leads.view`
- `PATCH /api/leads/{lead_id}` — `leads.update`
- `DELETE /api/leads/{lead_id}?row_version=2` — `leads.delete`

Create example:

```json
{
  "first_name": "Jane",
  "last_name": "Doe",
  "email": "jane@example.com",
  "company": "Example Company",
  "country": "Bangladesh",
  "company_website": "google.com",
  "service_requested": "Website Design",
  "email_status": "unknown",
  "phone": "+8801000000000",
  "company_size": "11_50",
  "revenue_amount": "100000.00",
  "revenue_currency": "USD",
  "revenue_period": "annual",
  "consent_status": "unknown",
  "assigned_user_id": null,
  "status": "new"
}
```

`tenant_id`, tracking/audit fields, source, import batch, and row version are server-controlled. An assignment must reference an active user membership in the selected workspace. Non-default assignment and status changes require `leads.assign` and `leads.status` respectively.

Update example:

```json
{
  "row_version": 1,
  "company": "Updated Company",
  "status": "qualified"
}
```

The current `row_version` is mandatory. A stale value returns `409 Conflict`. Status transitions are:

- `new` → `qualified`, `contacted`, or `disqualified`
- `qualified` → `contacted` or `disqualified`
- `contacted` → `engaged` or `disqualified`
- `engaged` → `converted` or `disqualified`
- `disqualified` → `new`
- `converted` is terminal

List query parameters include bounded `page`/`size` (`size <= 100`), `search`, `name`, `email`, `company`, `country`, `industry`, `service_requested`, `status`, `email_status`, `source`, and `assigned_user_id`. The country filter accepts the same names, aliases, and ISO codes as create/update. `service_requested` is trimmed and uses a case-insensitive full-value match; an empty value disables that filter. All supplied filters are combined with AND and the returned `total` reflects the complete filtered result before pagination.

Service dropdown values are available from:

```http
GET /api/leads/service-requested-options?search=web&page=1&size=50
Authorization: Bearer <workspace-token>
```

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "label": "Website Design",
        "value": "website design"
      }
    ],
    "page": 1,
    "size": 50,
    "total": 1
  }
}
```

Options are derived from active leads visible to the authenticated workspace membership. Blank values and archived leads are omitted, case variants are deduplicated, and staff only see options from leads assigned to them. `search` is optional and case-insensitive; `page >= 1` and `size` is bounded to 1–100.

Active email duplicates are rejected per workspace and return the authorized existing lead ID/tracking ID. Archived leads do not block a later intentional inquiry. Deletes archive records, cancel pending customer actions, and preserve lead events and audit history.

### Validation catalogs

- Email status: `unknown`, `unverified`, `valid`, `invalid`, `bounced`, `risky`, `disposable`
- Company size: `self_employed`, `1_10`, `11_50`, `51_200`, `201_500`, `501_1000`, `1001_5000`, `5001_10000`, `10000_plus`
- Revenue period: `monthly`, `quarterly`, `annual`
- Consent status: `unknown`, `not_requested`, `granted`, `denied`, `withdrawn`
- Country: recognized country name, common explicit alias, or ISO alpha-2/alpha-3 code; responses use canonical display names
- Revenue currency: ISO 4217 code

Country input is case-insensitive and surrounding whitespace is ignored. Ambiguous or unknown country names are rejected rather than guessed. Existing ISO-code data remains compatible because the database continues to store alpha-2 codes internally.

`company_website`, `linkedin_url`, `facebook_url`, `instagram_url`, `x_url`, and `github_url` are optional. Bare domains and `www` domains are normalized to HTTPS; existing HTTP/HTTPS URLs retain their scheme. Credentials, internal whitespace, malformed public hostnames, and non-HTTP schemes are rejected. Submitted URLs are never fetched. `service_requested` is optional, trimmed, limited to 200 characters, and a blank value becomes null.

Revenue amount, currency, and period must be supplied together. Granted consent requires evidence or a reference. Email syntax never changes `email_status` from `unknown`, and manual/import creation never schedules customer communication.

### Excel import

- `GET /api/leads/import-template`
- `POST /api/leads/imports/preview`
- `POST /api/leads/imports/{batch_id}/commit`
- `GET /api/leads/imports/{batch_id}`
- `GET /api/leads/imports/{batch_id}/errors?page=1&size=50`

Preview uses multipart form data:

- `file`: `.xlsx` workbook
- `mapping`: optional JSON object mapping source headers to canonical fields, for example `{"Given Name":"first_name"}`

Preview persists validated row snapshots and errors but creates no leads. Commit revalidates permission and active duplicates, inserts valid rows, skips duplicates, and reports invalid/failed rows. Commits are transactionally safe and idempotent; retrying a completed batch does not duplicate leads.

Template/import columns:

```text
first_name, last_name, email, designation, company, country, email_status,
linkedin_url, facebook_url, instagram_url, x_url, github_url, phone, city,
state, company_website, industry, service_requested, company_size, revenue_amount,
revenue_currency, revenue_period, notes, consent_status, consent_evidence,
consent_reference
```

Only `first_name`, `last_name`, and `email` are required. Older workbooks without `service_requested` remain valid. Human-readable headers such as `Service Requested` are normalized to `service_requested`. The template sample uses `google.com`, `Bangladesh`, and `Website Design`. Formula cells are rejected and never evaluated. Only `.xlsx` is accepted; macro workbooks are rejected, external links are not loaded, and submitted URLs are not fetched. Phone cells must be stored as text.

Import limits are environment-configurable:

```dotenv
LEAD_IMPORT_MAX_FILE_BYTES=5242880
LEAD_IMPORT_MAX_ROWS=5000
LEAD_IMPORT_RETENTION_HOURS=72
```

Imports are intentionally bounded and processed synchronously because this project has no durable-job infrastructure. The uploaded file is held only in memory and is not saved. Validated row snapshots expire after the configured retention period. Clean expired snapshots while retaining batch summaries/audit evidence with:

```powershell
python -m app.commands.cleanup_lead_imports --limit 1000
```

### Lead permissions

- `leads.view`
- `leads.create`
- `leads.update`
- `leads.delete`
- `leads.export`
- `leads.import`
- `leads.assign`
- `leads.status`

Owner and Operator receive lead-management permissions. Staff retain view/update access only to assigned leads. Readonly users cannot modify or import leads. Backend dependencies enforce these permissions independently of menu visibility.

### Voice lead draft extraction

`POST /api/leads/voice-extract` accepts multipart form data with one `audio` file. It requires an active workspace membership and the Lead menu access permission `leads.view`. It transcribes the audio and returns an editable draft only; it does not create a lead, send a message, set consent, or set server-controlled lead fields. Lead create/update/delete permissions remain unchanged.

Supported file types are FLAC, MP3/MPEG/MPGA, MP4/M4A, OGG, WAV, and WebM. Browser MIME parameters such as `audio/webm;codecs=opus` are accepted after validating the base media type.

Example response:

```json
{
  "transcript": "My name is Jane Doe, mobile 01712345678...",
  "extracted_fields": {
    "first_name": "Jane",
    "last_name": "Doe",
    "email": null,
    "phone": "01712345678",
    "country": "Bangladesh",
    "designation": null,
    "company": null,
    "city": null,
    "state": null,
    "company_website": null,
    "industry": null,
    "company_size": null,
    "revenue_amount": null,
    "revenue_currency": null,
    "revenue_period": null,
    "linkedin_url": null,
    "facebook_url": null,
    "instagram_url": null,
    "x_url": null,
    "github_url": null,
    "service_requested": null,
    "notes": null
  },
  "field_errors": [],
  "review_warnings": []
}
```

Configure Groq only in the backend environment:

```dotenv
GROQ_API_KEY=<server-side-secret>
GROQ_TRANSCRIPTION_MODEL=whisper-large-v3-turbo
GROQ_EXTRACTION_MODEL=openai/gpt-oss-20b
GROQ_REQUEST_TIMEOUT_SECONDS=30
LEAD_VOICE_MAX_UPLOAD_BYTES=10485760
LEAD_VOICE_MAX_DURATION_SECONDS=120
LEAD_VOICE_RATE_LIMIT_REQUESTS=10
LEAD_VOICE_RATE_LIMIT_WINDOW_SECONDS=60
```

The upload is size-bounded while being read and FastAPI's spooled temporary file is always closed. Duration is checked from Groq's verbose transcription metadata before structured extraction. The local sliding-window rate limit is per application process; use a shared limiter such as Redis when deploying multiple workers. Provider errors are returned as safe `422`, `429`, `502`, `503`, or `504` responses without exposing the API key or provider payload.

## Forgot password

`POST /api/auth/forgot-password`

```json
{
  "email": "user@gmail.com"
}
```

The endpoint always returns `200 OK`, whether or not the account exists:

```json
{
  "success": true,
  "message": "If the email exists, a password reset link has been sent."
}
```

## Reset password

`POST /api/auth/reset-password`

```json
{
  "token": "token-from-reset-link",
  "new_password": "new-password",
  "confirm_password": "new-password"
}
```

Success (`200 OK`):

```json
{
  "success": true,
  "message": "Password reset successful"
}
```

Invalid, expired, or previously used tokens receive the same safe `400 Bad Request` response. Raw reset tokens and passwords are never persisted or logged.

## Email delivery

`app/core/email.py` defines the email provider contract and currently uses a no-op development provider. It does not log recipient addresses, tokens, or reset links.

To add SendGrid, AWS SES, or SMTP:

1. Implement the `EmailProvider` protocol in a dedicated adapter.
2. Read provider credentials from environment variables or a production secret manager.
3. Wrap provider failures as `EmailDeliveryError`.
4. Select the production adapter during application startup.

Do not commit provider credentials to `.env.example` or source control.

## Tests

```powershell
pytest
```

Tests use an isolated in-memory database and do not change PostgreSQL data.
"# Power-Lead-Response-V1" 
