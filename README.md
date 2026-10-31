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

## Mail sender accounts

Sender accounts are scoped to the workspace selected by the bearer token. Clients never submit a tenant ID. The Mail menu uses three independent permissions:

- `mail.view` — list and read sender-account configuration.
- `mail.manage` — create, update, deactivate, delete, and set a default sender.
- `mail.test` — authenticate an SMTP connection or explicitly send a test email.

The migration grants these permissions only to the system Owner role. Other system or custom roles receive no Mail access until an Owner explicitly updates their menu permissions. Every non-view Mail action also requires `mail.view` when assigned through Menu Permission.

Endpoints:

- `GET /api/mail/sender-accounts?page=1&size=20&include_inactive=true`
- `POST /api/mail/sender-accounts`
- `GET /api/mail/sender-accounts/{sender_account_id}`
- `PATCH /api/mail/sender-accounts/{sender_account_id}`
- `POST /api/mail/sender-accounts/{sender_account_id}/deactivate`
- `DELETE /api/mail/sender-accounts/{sender_account_id}`
- `POST /api/mail/sender-accounts/{sender_account_id}/set-default`
- `POST /api/mail/sender-accounts/{sender_account_id}/test-connection`
- `POST /api/mail/sender-accounts/{sender_account_id}/test-email`

Create a custom SMTP sender:

```json
{
  "display_name": "Sales Team",
  "sender_email": "sales@example.com",
  "provider": "custom_smtp",
  "authentication_type": "password",
  "smtp_host": "smtp.example.com",
  "smtp_port": 587,
  "encryption": "STARTTLS",
  "smtp_username": "sales@example.com",
  "credential": "server-password-or-provider-token",
  "reply_to": "replies@example.com",
  "is_active": true,
  "is_default": true
}
```

Create a Gmail sender:

```json
{
  "display_name": "Sales Team",
  "sender_email": "sales@gmail.com",
  "provider": "gmail",
  "authentication_type": "app_password",
  "credential": "google-app-password",
  "encryption": "STARTTLS"
}
```

Gmail uses `smtp.gmail.com`; the defaults are port 587 with STARTTLS (or port 465 with TLS). The credential must be a Google App Password for an account where Google supports App Passwords. A normal Gmail account password is not accepted or recommended. OAuth is not implemented and must not be selected as an authentication type.

Update requests may omit `credential` to preserve the encrypted credential already stored. Sending a JSON `null` or blank credential is rejected. API responses never return the credential or its ciphertext; they return only `credentials_configured`.

Example response data:

```json
{
  "id": "sender-account-uuid",
  "display_name": "Sales Team",
  "sender_email": "sales@example.com",
  "provider": "custom_smtp",
  "authentication_type": "password",
  "smtp_host": "smtp.example.com",
  "smtp_port": 587,
  "encryption": "STARTTLS",
  "smtp_username": "sales@example.com",
  "credentials_configured": true,
  "reply_to": "replies@example.com",
  "is_active": true,
  "is_default": true,
  "connection_status": "not_tested",
  "last_tested_at": null,
  "last_test_message": null,
  "created_at": "2026-10-11T12:00:00Z",
  "updated_at": "2026-10-11T12:00:00Z"
}
```

Connection testing performs SMTP authentication and sends no email. It records only a sanitized success/failure status and time. Sending a test email is a separate, rate-limited request:

```json
{
  "recipient": "recipient@example.com"
}
```

Saving an account never opens an SMTP connection or sends email. SMTP operations use verified TLS certificates, bounded timeouts/concurrency, public-address DNS validation, and an IP-pinned connection. Loopback, private, link-local, reserved, metadata, and DNS-rebinding destinations are rejected. Supported SMTP ports are 25, 465, 587, and 2525, and authentication is never attempted without STARTTLS or TLS.

Only one default sender may exist per workspace; this is transactionally enforced and backed by a partial unique database index. Deactivation clears default status. If an installed message-history table references a sender through `sender_account_id`, delete deactivates the account instead, retaining the account and audit history.

Required server-side environment configuration:

```dotenv
SMTP_CREDENTIAL_ENCRYPTION_KEY=
SMTP_CREDENTIAL_PREVIOUS_KEYS=
SMTP_CONNECTION_TIMEOUT_SECONDS=10
SMTP_MAX_CONCURRENT_OPERATIONS=5
SMTP_TEST_EMAIL_RATE_LIMIT_REQUESTS=5
SMTP_TEST_EMAIL_RATE_LIMIT_WINDOW_SECONDS=300
```

`SMTP_CREDENTIAL_ENCRYPTION_KEY` is required before SMTP credentials can be saved or used. Its value must be one Fernet key: URL-safe Base64 encoding of exactly 32 random bytes (normally a 44-character string). Do not use an arbitrary password, JWT secret, Gmail App Password, quoted value, or a value containing a newline.

Generate a compatible Fernet key locally:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the generated value directly into the server secret setting. The command prints a newly generated encryption key only; it never reads or prints an existing key or SMTP credential.

### Render setup

In the Render dashboard, open the backend Web Service, select **Environment**, and add:

```text
Key:   SMTP_CREDENTIAL_ENCRYPTION_KEY
Value: <the complete generated Fernet key, without quotes>
```

Save the environment change and allow Render to redeploy/restart the service. Environment variables are read when the FastAPI process starts, so an already-running process must restart before the new value is available. `pydantic-settings` reads this uppercase process variable directly, case-insensitively, and process environment values take precedence over the local `.env` file. A production `.env` file is therefore not required on Render.

Do not put the generated value in `.env.example`, Git, frontend environment files, build arguments, or Angular. `.env.example` intentionally contains only an empty placeholder.

Store the real encryption key in the deployment secret manager, not source control or Angular. Back it up separately and securely: losing every copy makes stored SMTP credentials unrecoverable. To rotate, deploy a new `SMTP_CREDENTIAL_ENCRYPTION_KEY`, retain old keys as a comma-separated `SMTP_CREDENTIAL_PREVIOUS_KEYS`, and re-save each credential so it is encrypted by the new primary key. Remove a previous key only after all affected credentials have been re-encrypted and verified. Never log either key.

The module exposes `app/services/sender_account_service.py` and the asynchronous SMTP provider for future workflow delivery. Lead/workflow messaging is intentionally not connected in this change; future sending must independently enforce approved templates, consent/suppression, tenant scope, idempotency, and stop rules.

## Mail templates

Mail templates are scoped to the workspace selected by the authenticated membership. The API never accepts a tenant ID and never sends an email during create, update, upload, approval, archive, or preview.

Permissions:

- `mail.templates.view` — list, read, preview, and retrieve authorized private assets.
- `mail.templates.manage` — create, update, archive, upload, and remove draft assets.
- `mail.templates.approve` — enter the approval endpoint; final approval also requires the current membership to have the system `owner` role.

The migration grants these permissions only to the system Owner role. They are not automatically granted to Operator, Staff, Readonly, or custom roles.

Endpoints:

- `GET /api/mail/templates/placeholders`
- `GET /api/mail/templates?page=1&size=20&search=&enabled=&approval_status=&include_archived=false`
- `POST /api/mail/templates`
- `GET /api/mail/templates/{template_id}?version=1&include_archived=false`
- `PATCH /api/mail/templates/{template_id}`
- `DELETE /api/mail/templates/{template_id}` — archives rather than physically deleting.
- `POST /api/mail/templates/{template_id}/approve`
- `POST /api/mail/templates/{template_id}/preview`
- `POST /api/mail/templates/{template_id}/assets`
- `GET /api/mail/templates/{template_id}/assets/{asset_id}/content`
- `DELETE /api/mail/templates/{template_id}/assets/{asset_id}`

Create request:

```json
{
  "name": "Initial Outreach",
  "subject": "Hello {{first_name}}",
  "body_html": "<p>Hi {{first_name}}, welcome to <strong>{{company}}</strong>.</p>",
  "signature_html": "<p>Regards,<br>Sales Team</p>",
  "enabled": true
}
```

`PATCH` accepts the same fields optionally. Names are trimmed and unique case-insensitively among active templates in the workspace. Subjects reject CR/LF header injection. HTML is sanitized server-side before persistence, and `body_text`/`signature_text` are derived from the sanitized HTML.

Allowed placeholder keys:

- `first_name`
- `last_name`
- `email`
- `phone`
- `company`
- `designation`
- `service_requested`
- `city`
- `state`
- `country`
- `industry`
- `company_website`
- `company_size`

Literal content such as `Hi Arif` is valid. Placeholder syntax is deliberately limited to `{{field_name}}`; arbitrary expressions, function calls, attribute access, malformed tokens, and unknown fields are rejected. Substituted values are HTML-escaped. Missing preview values remain visibly unresolved and are returned in `missing_values`; no replacement is invented.

Preview with sample values:

```json
{
  "sample_values": {
    "first_name": "Arif",
    "company": "PowerLead"
  },
  "version": 1
}
```

Preview with an authorized lead:

```json
{
  "lead_id": "lead_uuid"
}
```

`lead_id` and `sample_values` are mutually exclusive. Lead lookup uses the existing tenant and assigned-lead authorization rules. The response contains `rendered_subject`, `rendered_body_html`, `rendered_signature_html`, combined `rendered_html`, `rendered_text`, `missing_values`, `warnings`, and authorized asset-content URLs. Preview never contacts SMTP.

### Template lifecycle

New templates start as version 1 with `approval_status=draft`. Only a workspace Owner with `mail.templates.approve` can approve the current draft. Approved rows and their asset links are immutable. Editing approved subject/body/signature or adding/removing an asset creates the next draft version and copies the prior version's asset references. Future workflow delivery must select an exact approved version; the reusable composer refuses draft versions. Archiving disables the template but preserves every version and asset reference for historical-message integrity.

### Asset uploads

Upload with multipart form data:

```text
POST /api/mail/templates/{template_id}/assets
kind: inline_body | signature_image | attachment
file: <binary file>
```

The response for an inline image supplies markup such as:

```html
<img data-asset-id="asset_uuid" alt="">
```

Insert that markup into `body_html` for `inline_body` or `signature_html` for `signature_image`. Preview returns authorized content URLs; raw object-storage URLs and filesystem paths are never exposed. MIME composition converts the asset ID into a Content-ID reference and adds HTML plus plain-text alternatives. `attachment` assets are normal MIME attachments.

Allowed inline/signature image contents: PNG, JPEG, GIF, and WebP. Allowed attachment contents: those images plus PDF, DOCX, XLSX, UTF-8 TXT, and CSV. Detection uses file signatures/container contents rather than trusting the submitted extension or MIME type. Executables, scripts, HTML, SVG, macro-bearing Office containers, embedded Office objects, unknown binary data, and remote image URLs are rejected. Files receive generated tenant-scoped object keys.

Defaults:

- Maximum individual asset: 10 MiB.
- Maximum assets per version: 20.
- Maximum estimated encoded message size: 25 MiB, including MIME Base64 overhead.
- API pagination size: 1–100.

Unreferenced draft assets are removed from object storage and PostgreSQL when explicitly deleted. Assets still referenced by an approved or historical version are retained. Uploaded image assets must be referenced by the correct body/signature HTML location before approval.

HTML supports paragraphs, line breaks, bold, italic, underline, ordered/unordered lists, safe HTTP/HTTPS/mailto links, text color, alignment, and uploaded images. Scripts, event handlers, unsafe elements, remote image sources, unsafe URL schemes, CSS expressions, and CSS URL fetching are rejected. Image-only signatures are valid when they reference an authorized uploaded signature asset.

### Persistent object storage

Template binaries are never written to Render's temporary filesystem. Configure a private S3-compatible bucket using server-side environment variables:

```dotenv
OBJECT_STORAGE_BUCKET=
OBJECT_STORAGE_REGION=us-east-1
OBJECT_STORAGE_ENDPOINT_URL=
OBJECT_STORAGE_ACCESS_KEY_ID=
OBJECT_STORAGE_SECRET_ACCESS_KEY=
OBJECT_STORAGE_USE_SSL=true
MAIL_TEMPLATE_ASSET_MAX_FILE_BYTES=10485760
MAIL_TEMPLATE_MAX_ASSETS=20
MAIL_TEMPLATE_MAX_TOTAL_BYTES=26214400
```

For AWS S3, `OBJECT_STORAGE_ENDPOINT_URL` may remain empty. For an S3-compatible provider, set its HTTPS endpoint. Keep access keys only in the backend secret manager; never expose them to Angular or commit them. The bucket must be persistent and private. Objects are accessed through the authenticated API, use generated keys, request server-side encryption, and are never made public.

Apply the template migration with:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

Current template migration revision: `c8d4f1a29b60`.

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

## Message templates

Plain-text Message Templates are tenant-scoped and separate from Email Templates.
They use `mail.templates.view` for list/detail/preview and
`mail.templates.manage` for create/update/archive. Routes are rooted at
`/api/message-templates`; preview accepts either an authorized `lead_id` or an
allowlisted `sample_values` object and never sends a message. Deletion archives the
record so IDs and audit history remain available. Message Templates have no subject,
signature, assets, SMTP sender, sending behavior, or Email Template approval lifecycle.

Apply the table migration with:

```powershell
alembic upgrade head
```

## Phase 7 outbound email

The original single-message flow remains a two-step operation. `POST
/api/mail/messages/preview` creates an expiring server-side snapshot and returns the
exact recipient, rendered subject, HTML, plain text and a content hash. `POST
/api/mail/messages/send` accepts that preview ID/hash plus an idempotency key and
transactionally creates an immutable message snapshot and durable outbox job. Neither
preview nor lead/customer save sends email.

Explicit Lead List sends use `POST /api/mail/batches/send`. This endpoint does not
require a prior preview and does not create outbox jobs. It resolves authorized leads,
renders immutable personalized snapshots, persists them, and calls the same protected
SMTP transport used by Sender Account test-email. It waits for the bounded direct
attempts and returns each recipient as `sent`, `failed`, `unknown`, or `skipped`.
SMTP acceptance means `sent`; it does not mean delivered or read. Timeout or ambiguous
acceptance is `unknown` and is never automatically retried. The default direct limit is
10 recipients per request, concurrency is 3, and the per-message timeout is 30 seconds
(`MAIL_DIRECT_SEND_MAX_RECIPIENTS`, `MAIL_DIRECT_SEND_CONCURRENCY`, and
`MAIL_DIRECT_SEND_TIMEOUT_SECONDS`). Existing preview/confirm endpoints and worker
jobs remain available for other consumers and workflow features.

`supersede_queued` defaults to `false`, preserving normal duplicate-send
protection. When an authorized user explicitly sets it to `true`, the direct-send
transaction may cancel an older worker-backed message only when that message is
still queued, its job has no lease, and no SMTP attempt has begun. The old message,
job, events, activity and audit history are retained. Processing, sent, unknown,
leased or previously attempted messages are never superseded.

If a browser disconnects, repeat the same request only with the same idempotency key,
or query `GET /api/mail/batches/{batch_id}` and `GET
/api/mail/batches/{batch_id}/recipients`. Reusing the key with changed content is
rejected. A replay returns the stored result and does not contact SMTP again. Direct
responses also report the workspace's existing `pending_worker_job_count`; those jobs
are not processed, changed, or retried by the direct endpoint.

Required permissions are `mail.send` for preview/enqueue, `mail.view` for list/status,
and `mail.cancel` for cancellation. Owner receives all permissions; operator receives
send/cancel by the migration. Record visibility and staff assigned-lead scope are still
enforced.

Run the API and durable worker as separate Render services using the same database,
storage and secret environment:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port $env:PORT
python -m app.commands.process_outbox
```

For a one-batch health/debug run use `python -m app.commands.process_outbox --once`.
On Render, configure the API as a Web Service and the worker as a Background Worker.
Do not run the worker as a one-off release command. Apply migrations before starting
the new services:

```powershell
alembic upgrade head
```

In addition to the existing SMTP encryption and object-storage variables, the worker
accepts `MAIL_PREVIEW_EXPIRE_MINUTES`, `MAIL_WORKER_POLL_SECONDS`,
`MAIL_WORKER_BATCH_SIZE`, `MAIL_WORKER_CONCURRENCY`, `MAIL_WORKER_LEASE_SECONDS`,
`MAIL_WORKER_MAX_ATTEMPTS`, `MAIL_WORKER_SENDER_RATE_PER_MINUTE`,
`MAIL_WORKER_WORKSPACE_RATE_PER_MINUTE`, and `MAIL_MAX_ENCODED_MESSAGE_BYTES`.
Real deployments must permit outbound DNS and SMTP traffic to the configured public
SMTP host/port. TLS certificate verification and the existing private/link-local/
metadata destination blocks remain enabled.

Eligibility (purpose, conversion state, consent, opt-out/suppression and email state)
is checked at preview/enqueue and again immediately before the provider call. SMTP
acceptance is recorded as `sent`, not delivered/read. An exception after the SMTP
send begins is recorded as `unknown` and is never automatically retried. Cancellation
works for queued/retry jobs; a provider call already in flight cannot be recalled, so
processing jobs return `409` and conversion records a cancellation request for the
worker's final pre-send check.

## Direct Lead SMS (Twilio Programmable Messaging)

Lead SMS uses Twilio Programmable Messaging through `POST /api/sms/send`; it does
not use Twilio Verify, create an outbox job, or require a preview. The server resolves
authorized leads, renders the tenant's existing plain-text Message Template separately
for every lead, stores the immutable body/template/sender snapshot, and then calls
Twilio with bounded concurrency. The default request limit is 10 recipients,
concurrency is 3, and each provider call has a 20-second timeout. Twilio may queue
messages according to the account and sender throughput limits.
Rendered SMS content is capped at 1,600 Unicode characters before submission;
carrier segmentation and billing remain provider concerns.

Configure these server-only values in `.env` or the hosting environment:

```dotenv
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_SMS_FROM=
SMS_DIRECT_SEND_MAX_RECIPIENTS=10
SMS_DIRECT_SEND_CONCURRENCY=3
SMS_DIRECT_SEND_TIMEOUT_SECONDS=20
SMS_STATUS_REFRESH_MIN_SECONDS=30
```

`TWILIO_SMS_FROM` is the Twilio sender used by Programmable Messaging. Lead phones
must already be in international E.164 form (for example `+14155552671`); the backend
does not infer a country code. Trial-recipient verification, sender capability,
geographic permissions, registration requirements, and content restrictions remain
Twilio/account responsibilities. A successful create call records Twilio's returned
`accepted`, `queued`, `sent`, or `delivered` state exactly; it never upgrades API
acceptance to delivery.

Use `GET /api/sms/batches/{batch_id}` after a browser timeout. Read an individual
record at `GET /api/sms/messages/{message_id}` and request a bounded provider lookup
with `POST /api/sms/messages/{message_id}/refresh-status`. Refresh calls are throttled
by `SMS_STATUS_REFRESH_MIN_SECONDS`; webhooks/tunnels are not required for local
status checks. Repeating a send with the same idempotency key returns its original
batch, while changed content with that key is rejected.

If the browser never received a batch ID, recover the original operation without
submitting another send request:

```text
GET /api/sms/operations/recover?idempotency_key=<original-key>
```

Recovery requires `mail.view`, is limited to the user who originated the operation,
and returns `missing`, `in_progress`, or `completed`. It never contacts Twilio and
never creates or retries a message. A pre-provider configuration failure returns HTTP
503 with `classification=pre_provider_failure` and `provider_request_made=false`.
Definitive Twilio rejections are stored as `failed`; transport timeouts or ambiguous
provider acceptance are stored as `unknown`. Provider acceptance is committed per
recipient, with a bounded database-only persistence retry that never repeats the
Twilio request.

SMS send uses the existing `mail.send` permission; batch/message status and refresh
use `mail.view`. Staff scope remains limited to assigned leads. Explicit denied or
withdrawn consent, archived/disqualified/converted lead state, invalid phone data,
and duplicate recipients remain blocked. Non-denied consent is not changed or
implicitly promoted to granted.
"# Power-Lead-Response-V1" 
## WhatsApp Sandbox inbox

The WhatsApp inbox uses Twilio Programmable Messaging and the existing tenant,
membership, permission and lead-activity infrastructure. It receives inbound
Sandbox messages, stores one workspace-scoped conversation per external WhatsApp
address, supports explicit free-form replies during the 24-hour customer-service
window, and records delivery callbacks. It does not send from the Lead List and
does not treat application Message Templates as WhatsApp-approved templates.

### Configuration

Keep real values in `.env` or the deployment secret manager. The repository
contains only blank placeholders in `.env.example`.

```dotenv
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_FROM=
TWILIO_WHATSAPP_WEBHOOK_BASE_URL=
WHATSAPP_REPLY_MAX_CHARACTERS=1600
WHATSAPP_REPLY_TIMEOUT_SECONDS=20
WHATSAPP_REPLY_RATE_LIMIT_REQUESTS=30
WHATSAPP_REPLY_RATE_LIMIT_WINDOW_SECONDS=60
```

`TWILIO_WHATSAPP_FROM` accepts `+E164` or `whatsapp:+E164`; the provider always
uses `whatsapp:+E164`. `TWILIO_WHATSAPP_WEBHOOK_BASE_URL` is the public HTTPS
origin without a trailing slash. It is used both for exact Twilio signature
validation behind a proxy and for the outbound status callback URL.

Apply the reviewed migration explicitly (the application does not run it):

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

With `LIVE=false`, map the configured Sandbox sender to one existing workspace.
The command is transactional and idempotent, and refuses a sender already mapped
to another workspace:

```powershell
.\.venv\Scripts\python.exe -m app.commands.map_whatsapp_channel --workspace-slug <exact-workspace-slug>
```

### Local tunnel and Twilio Sandbox

1. Run the API: `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload`.
2. Expose it, for example with `ngrok http 8000`.
3. Set `TWILIO_WHATSAPP_WEBHOOK_BASE_URL=https://<your-ngrok-host>` and restart.
4. In the Twilio WhatsApp Sandbox configuration, set **When a message comes in**
   to the following HTTP `POST` URL:

   `https://<your-ngrok-host>/api/webhooks/twilio/whatsapp/inbound`

Outbound replies automatically pass this status callback URL:

`https://<your-ngrok-host>/api/webhooks/twilio/whatsapp/status`

Both webhook endpoints validate `X-Twilio-Signature` using the complete external
URL and all form parameters. They do not accept application JWTs. Do not put a
query string in the Twilio Console URL unless the same query string reaches the
application exactly.

### Permissions

- `messages.view`: list conversations/history and mark a conversation read.
- `messages.send`: send explicit replies, recover send operations and link a
  conversation to an authorized lead/customer.

The migration grants both only to the system Owner role. Other roles receive no
automatic WhatsApp access. An Owner can grant them through the existing Menu
Permission screen/API: read the complete current set with
`GET /api/settings/roles/{role_id}/permissions`, then send the complete updated
set to `PUT /api/settings/roles/{role_id}/permissions` as
`{"permission_keys":[...,"messages.view","messages.send"]}`. The existing rule
requires `messages.view` whenever `messages.send` is granted.

### Authenticated API

- `GET /api/messages/whatsapp/conversations?page=1&size=20&search=`
- `GET /api/messages/whatsapp/conversations/{conversation_id}/messages?page=1&size=50`
- `POST /api/messages/whatsapp/conversations/{conversation_id}/reply`
- `POST /api/messages/whatsapp/conversations/{conversation_id}/read`
- `PUT /api/messages/whatsapp/conversations/{conversation_id}/link`
- `GET /api/messages/whatsapp/operations/recover?idempotency_key=<key>`

Reply request:

```json
{
  "body": "Thanks for contacting us.",
  "idempotency_key": "wa-reply-20261022-0001"
}
```

Reply success data includes `message`, `replayed`, and `operation_state`. The
message carries its provider status (`accepted`, `queued`, `sending`, `sent`,
`delivered`, `read`, `failed`, `undelivered`, or `unknown`) separately from the
application submission state. Accepted/queued never means delivered.

When the free-form window is closed, the API returns HTTP `409` with code
`approved_template_required` and does not call Twilio. Reusing an idempotency key
with changed content returns HTTP `409` with code `idempotency_conflict`. The
recovery endpoint returns `missing`, `in_progress`, or `completed` without
submitting another provider request.

Link request (exactly one field):

```json
{"lead_id":"<authorized-lead-uuid>"}
```

or

```json
{"customer_id":"<authorized-customer-uuid>"}
```

Incoming media URLs are never exposed or retained. Authorized clients receive
only safe media metadata and an unsupported-media placeholder until media support
is implemented.

## Meta WhatsApp Cloud API inbox

Meta Cloud API is a separate provider integration. It does not replace, merge or
fall back to the Twilio WhatsApp channel. Conversations are keyed by workspace,
provider channel and external contact, so the same phone number can have separate
Twilio and Meta histories.

### Configuration and workspace mapping

Store real values only in `.env` or the deployment secret manager:

```dotenv
META_WHATSAPP_ACCESS_TOKEN=
META_WHATSAPP_PHONE_NUMBER_ID=
META_WHATSAPP_WABA_ID=
META_APP_SECRET=
META_WEBHOOK_VERIFY_TOKEN=
META_GRAPH_API_VERSION=v26.0
META_WHATSAPP_TIMEOUT_SECONDS=20
META_WHATSAPP_SEND_RATE_LIMIT_REQUESTS=30
META_WHATSAPP_SEND_RATE_LIMIT_WINDOW_SECONDS=60
META_WHATSAPP_TEMPLATE_FETCH_LIMIT=500
```

`META_APP_SECRET`, the access token and verify token are server-only and are never
returned by an API. `META_GRAPH_API_VERSION` is configurable so a supported Graph
version can be advanced without changing code.

Apply the reviewed migration explicitly; application startup never runs it:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Map the configured WABA ID and Phone Number ID to the existing workspace. The
command is transactional and idempotent, and does not alter the Twilio mapping:

```powershell
.\.venv\Scripts\python.exe -m app.commands.map_meta_whatsapp_channel --workspace-slug <exact-workspace-slug> --sender-address <+E164-Meta-sender>
```

### Meta webhook setup

1. Expose the API on a stable public HTTPS origin.
2. In the Meta app dashboard, open WhatsApp/Webhooks configuration and set the
   callback URL to `https://<host>/api/webhooks/meta/whatsapp`.
3. Enter the same server-side value configured as `META_WEBHOOK_VERIFY_TOKEN`.
   Meta's `GET` verification receives the exact challenge as plain text.
4. Subscribe the WhatsApp Business Account to the app and subscribe the
   `messages` webhook field. WABA app subscription can be checked/created using
   Graph API `GET/POST /<WABA_ID>/subscribed_apps` with an appropriately
   authorized token.
5. Confirm inbound and status callbacks contain the configured WABA ID and Phone
   Number ID. The backend rejects any other mapping.

Every `POST` webhook is validated from the raw request bytes with
`X-Hub-Signature-256` and `META_APP_SECRET`. Messages/status events are deduplicated
in PostgreSQL. Unsupported media creates a safe placeholder; the backend neither
downloads remote media nor automatically replies or creates a lead.
The verification token is redacted from the server access-log query string.

Meta's development/test phone number is restricted to the recipients configured
in the app's API Setup test-recipient list. Production behavior requires a properly
registered and active business phone number, suitable access token/permissions,
business/account eligibility and compliance with WhatsApp messaging/opt-in and
template rules. The application does not bypass these provider restrictions.

### Permissions and APIs

The Meta routes reuse `messages.view` and `messages.send`; no new role grant is
created. Staff remains limited to linked leads/customers in assigned-lead scope.

```text
GET  /api/messages/meta/whatsapp/channel
GET  /api/messages/meta/whatsapp/conversations?page=1&size=20&search=
POST /api/messages/meta/whatsapp/conversations/open
GET  /api/messages/meta/whatsapp/conversations/{id}/messages?page=1&size=50
POST /api/messages/meta/whatsapp/conversations/{id}/read
PUT  /api/messages/meta/whatsapp/conversations/{id}/link
GET  /api/messages/meta/whatsapp/templates?page=1&size=20&search=
POST /api/messages/meta/whatsapp/send
GET  /api/messages/meta/whatsapp/operations/recover?idempotency_key=<key>
GET  /api/webhooks/meta/whatsapp
POST /api/webhooks/meta/whatsapp
```

Opening/finding a conversation never sends:

```json
{"phone_number":"+14155552671"}
```

Its response includes `eligibility.free_form_allowed`,
`service_window_expires_at`, `approved_template_required`,
`template_outreach_allowed` and structured restrictions calculated by the server.

Explicit service-window text send:

```json
{
  "conversation_id": "<conversation-uuid>",
  "mode": "text",
  "text": "Thanks for contacting us.",
  "purpose": "lead_acquisition",
  "idempotency_key": "meta-wa-20261024-0001"
}
```

Explicit approved-template send:

```json
{
  "phone_number": "+14155552671",
  "mode": "approved_template",
  "template": {
    "name": "lead_intro",
    "language": "en_US",
    "parameters": [
      {"component":"body","index":1,"value":"Arif"},
      {"component":"body","index":2,"value":"PowerLead"}
    ]
  },
  "purpose": "lead_acquisition",
  "idempotency_key": "meta-wa-20261024-0002"
}
```

The server fetches templates from the configured WABA, requires exact name and
language, permits only provider status `APPROVED`, and validates the complete
parameter positions against the provider definition. Local Message Templates are
not treated as Meta-approved templates and arbitrary browser-rendered template
content is not accepted.

Successful send processing returns the persisted message, `replayed`, and
`operation_state`. Provider acceptance is stored as `accepted`; it is not described
as delivered/read until a matching webhook status is persisted. Definitive Meta
rejections become `failed`; ambiguous network/response outcomes become `unknown`
and are never retried automatically or sent through Twilio.

Representative safe errors:

```json
{
  "detail": {
    "code": "approved_template_required",
    "message": "The customer-service window is closed; use an approved Meta template",
    "service_window_expires_at": "2026-10-24T12:00:00+00:00"
  }
}
```

```json
{
  "detail": {
    "code": "whatsapp_opt_in_required",
    "message": "Business-initiated WhatsApp templates require recorded opt-in"
  }
}
```

Other stable codes include `invalid_phone_number`, `recipient_mismatch`,
`template_not_found`, `template_not_approved`, `invalid_template_parameters`,
`messaging_consent_denied`, `customer_suppressed`, `channel_restricted`,
`customer_purpose_required`, `idempotency_conflict`, and
`meta_whatsapp_channel_not_configured`.

An idempotency key is unique within a workspace operation. Repeating the identical
request returns the original record without a provider call; changed content is a
conflict. Recovery is read-only and returns `missing`, `in_progress`, or `completed`:

```json
{
  "success": true,
  "data": {
    "idempotency_key": "meta-wa-20261024-0001",
    "found": true,
    "operation_state": "completed",
    "message": {"id":"<message-uuid>","status":"accepted","provider_message_id":"wamid..."}
  }
}
```

## Facebook Page Messenger webhook

Messenger webhook ingestion reuses the tenant-scoped messaging channel,
conversation, message and delivery-event storage. Page-scoped user IDs remain
scoped to the mapped Page channel and are not merged with WhatsApp phone
identities or contacts by name.

Configure secrets only in `.env` or the deployment secret manager:

```dotenv
META_MESSENGER_VERIFY_TOKEN=
META_MESSENGER_PAGE_ID=
META_MESSENGER_PAGE_ACCESS_TOKEN=
META_MESSENGER_APP_SECRET=
META_MESSENGER_TIMEOUT_SECONDS=20
META_MESSENGER_REPLY_MAX_CHARACTERS=2000
META_MESSENGER_REPLY_RATE_LIMIT_REQUESTS=30
META_MESSENGER_REPLY_RATE_LIMIT_WINDOW_SECONDS=60
META_MESSENGER_PROFILE_CACHE_HOURS=24
META_MESSENGER_PROFILE_RETRY_SECONDS=900
META_MESSENGER_PROFILE_REFRESH_LIMIT=25
META_MESSENGER_PROFILE_REFRESH_CONCURRENCY=4
META_MESSENGER_PROFILE_REFRESH_RATE_LIMIT_REQUESTS=5
META_MESSENGER_PROFILE_REFRESH_RATE_LIMIT_WINDOW_SECONDS=60
```

`META_MESSENGER_APP_SECRET` takes precedence. It may be left blank only when
Messenger and WhatsApp use the same Meta app, in which case `META_APP_SECRET` is
reused. `META_GRAPH_API_VERSION` is also used for Messenger Send API calls.

Apply the reviewed migration explicitly; application startup does not apply it:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Map the configured Page ID to one existing workspace. The command is
transactional and idempotent and will not move a Page already owned by another
workspace:

```powershell
.\.venv\Scripts\python.exe -m app.commands.map_meta_messenger_page --workspace-slug <exact-workspace-slug>
```

Configure the Meta Page webhook callback as:

```text
https://<host>/api/webhooks/meta/messenger
```

Use `META_MESSENGER_VERIFY_TOKEN` for callback verification and subscribe the
Page webhook to `messages`, `message_echoes`, `message_deliveries` and
`message_reads`. The backend accepts text and attachment-bearing message events,
stores safe attachment placeholders without fetching remote URLs, persists and
reconciles echo/delivery/read events, deduplicates provider identifiers, and
safely ignores other valid event types. Webhook endpoints do not require an
application JWT:

```text
GET  /api/webhooks/meta/messenger
POST /api/webhooks/meta/messenger
```

Authenticated inbox APIs use the existing `messages.view` and `messages.send`
permissions. They derive the workspace and Page-scoped recipient from the
selected membership/conversation; a client cannot provide a tenant ID or PSID
for a reply:

```text
GET  /api/messages/meta/messenger/channel
POST /api/messages/meta/messenger/profiles/refresh
GET  /api/messages/meta/messenger/conversations?page=1&size=20&search=
GET  /api/messages/meta/messenger/conversations/{conversation_id}/messages?page=1&size=50
POST /api/messages/meta/messenger/conversations/{conversation_id}/read
PUT  /api/messages/meta/messenger/conversations/{conversation_id}/link
POST /api/messages/meta/messenger/conversations/{conversation_id}/reply
GET  /api/messages/meta/messenger/operations/recover?idempotency_key=...
```

Inbox reads use cached display names and never issue one provider request per
row. To populate or refresh an existing Page and up to 25 accessible contacts,
call the rate-limited refresh endpoint with `messages.view`:

```json
{
  "conversation_ids": ["<conversation-uuid>"],
  "refresh_page": true
}
```

An empty `conversation_ids` list refreshes a bounded set of unresolved visible
conversations. Successful Page/contact names are cached for 24 hours. Restricted
or unavailable profiles receive a 15-minute retry backoff. Failed lookups leave
the inbox usable with `Facebook Page` and `Messenger user` fallbacks. A linked
lead/customer name always takes precedence over provider profile data.

The provider requests only Page `name` and Messenger user `first_name,last_name`.
The configured token must belong to the mapped Page. Meta conversation/profile
access can require `pages_manage_metadata`, `pages_read_engagement`,
`pages_messaging`, a Page task that permits messaging/moderation, and Advanced
Access/Business Asset User Profile Access for people without an app/Page role.
Unavailable fields are treated as restricted rather than inferred.

Reply example (text is preserved exactly and is limited to 2,000 characters):

```json
{
  "text": "Thanks for contacting us.",
  "idempotency_key": "messenger-reply-20261026-0001"
}
```

Replies use `messaging_type=RESPONSE` and are allowed only while the standard
24-hour window calculated from the last qualifying inbound user message is
open. Echoes, receipts and CRM read cursors never open that window. This phase
does not use message tags, `HUMAN_AGENT`, cold outreach, bulk sending or
`mark_seen`. Provider acceptance is stored as `accepted`, not delivered/read.
Use the GET-only recovery endpoint after a browser timeout; it never calls Meta
or retries an unknown outcome.

## Meta Instagram configuration

Instagram API with Unified Login configuration is loaded only through
`app.core.config.Settings`. Local development reads `.env`; a deployed Render
process supplies the same names as process environment variables. Pydantic
Settings gives process variables precedence over `.env` values.

Required Instagram variables:

- `META_INSTAGRAM_CONFIG_ID`: Unified Login configuration ID from Meta App Dashboard.
- `META_INSTAGRAM_APP_ID`: Meta app ID used as the Unified Login OAuth client ID.
- `META_INSTAGRAM_ACCOUNT_ID`: Instagram Professional Account ID assigned to the token.
- `META_INSTAGRAM_ACCESS_TOKEN`: server-only Business Integration System User token.
- `META_INSTAGRAM_VERIFY_TOKEN`: private webhook challenge token.
- `META_INSTAGRAM_APP_SECRET`: Instagram app secret for webhook signature validation.
- `META_INSTAGRAM_OAUTH_REDIRECT_URI`: exact public HTTPS callback registered on the
  Unified Login configuration, for example
  `https://api.example.com/api/integrations/meta/instagram/oauth/callback`.
- `META_GRAPH_API_VERSION`: shared supported Meta Graph API version.

Instagram reconnection uses `POST /api/messages/meta/instagram/connect/initiate`
with `settings.integrations.update`. The returned Meta authorization URL must be
opened by the workspace administrator. Meta redirects to the dedicated callback:

```text
GET /api/integrations/meta/instagram/oauth/callback
```

The callback is not the Instagram webhook. It validates a one-time expiring state,
exchanges the authorization code server-side, verifies that the configured
professional account appears in `/me/assigned_instagram_accounts`, and encrypts the
token on the tenant channel using `SMTP_CREDENTIAL_ENCRYPTION_KEY`. Codes, state and
tokens are never returned or written to access logs. Run migration
`b7e2c41d8f90` before using this flow.

Never commit real IDs or secrets to `.env.example`. Missing account/token settings
leave sending unavailable, and missing webhook settings cause safe webhook rejection.

Map the account currently selected by `META_INSTAGRAM_ACCOUNT_ID`:

```powershell
.\.venv\Scripts\python.exe -m app.commands.map_meta_instagram_account `
  --workspace-slug <workspace-slug>
```

Preview a mapping correction from an explicitly identified old account to the account
currently selected by `META_INSTAGRAM_ACCOUNT_ID` (the default is read-only):

```powershell
.\.venv\Scripts\python.exe -m app.commands.correct_meta_instagram_account `
  --workspace-slug <workspace-slug> `
  --old-account-id <old-instagram-account-id>
```

After reviewing the dry-run result, apply it explicitly:

```powershell
.\.venv\Scripts\python.exe -m app.commands.correct_meta_instagram_account `
  --workspace-slug <workspace-slug> `
  --old-account-id <old-instagram-account-id> `
  --apply
```

Neither command runs during application startup.
