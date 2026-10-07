from enum import StrEnum


class RoleKey(StrEnum):
    OWNER = "owner"
    OPERATOR = "operator"
    STAFF = "staff"
    READONLY = "readonly"


class PermissionKey(StrEnum):
    TENANT_USERS_MANAGE = "tenant.users.manage"
    TENANT_SETTINGS_MANAGE = "tenant.settings.manage"
    TEMPLATES_READ = "templates.read"
    TEMPLATES_DRAFT = "templates.draft"
    TEMPLATES_APPROVE = "templates.approve"
    WORKFLOWS_READ = "workflows.read"
    WORKFLOWS_DRAFT = "workflows.draft"
    LEADS_READ = "leads.read"
    LEADS_MANAGE = "leads.manage"
    LEADS_ASSIGNED_UPDATE = "leads.assigned.update"
    AUDITS_READ = "audits.read"
    AUDITS_MANAGE = "audits.manage"
    DASHBOARD_READ = "dashboard.read"
    INTEGRATIONS_USE = "integrations.use"
    OVERVIEW_VIEW = "overview.view"
    LEADS_VIEW = "leads.view"
    LEADS_CREATE = "leads.create"
    LEADS_UPDATE = "leads.update"
    LEADS_DELETE = "leads.delete"
    LEADS_EXPORT = "leads.export"
    LEADS_IMPORT = "leads.import"
    LEADS_ASSIGN = "leads.assign"
    LEADS_STATUS = "leads.status"
    WORKFLOWS_VIEW = "workflows.view"
    WORKFLOWS_CREATE = "workflows.create"
    WORKFLOWS_UPDATE = "workflows.update"
    WORKFLOWS_DELETE = "workflows.delete"
    WORKFLOWS_ACTIVATE = "workflows.activate"
    WORKFLOWS_APPROVE = "workflows.approve"
    AUDITS_VIEW = "audits.view"
    AUDITS_CREATE = "audits.create"
    AUDITS_UPDATE = "audits.update"
    AUDITS_DELETE = "audits.delete"
    AUDITS_EXPORT = "audits.export"
    TEMPLATES_VIEW = "templates.view"
    TEMPLATES_CREATE = "templates.create"
    TEMPLATES_UPDATE = "templates.update"
    TEMPLATES_DELETE = "templates.delete"
    SETTINGS_ROLES_VIEW = "settings.roles.view"
    SETTINGS_ROLES_CREATE = "settings.roles.create"
    SETTINGS_ROLES_UPDATE = "settings.roles.update"
    SETTINGS_ROLES_DELETE = "settings.roles.delete"
    SETTINGS_USERS_VIEW = "settings.users.view"
    SETTINGS_USERS_CREATE = "settings.users.create"
    SETTINGS_USERS_UPDATE = "settings.users.update"
    SETTINGS_USERS_DELETE = "settings.users.delete"
    SETTINGS_USERS_RESET_PASSWORD = "settings.users.reset_password"
    SETTINGS_MENU_PERMISSIONS_VIEW = "settings.menu_permissions.view"
    SETTINGS_MENU_PERMISSIONS_UPDATE = "settings.menu_permissions.update"
    SETTINGS_INTEGRATIONS_VIEW = "settings.integrations.view"
    SETTINGS_INTEGRATIONS_UPDATE = "settings.integrations.update"


ROLE_PERMISSIONS: dict[RoleKey, frozenset[PermissionKey]] = {
    RoleKey.OWNER: frozenset(PermissionKey),
    RoleKey.OPERATOR: frozenset(
        {
            PermissionKey.TEMPLATES_READ,
            PermissionKey.TEMPLATES_DRAFT,
            PermissionKey.WORKFLOWS_READ,
            PermissionKey.WORKFLOWS_DRAFT,
            PermissionKey.LEADS_READ,
            PermissionKey.LEADS_MANAGE,
            PermissionKey.AUDITS_READ,
            PermissionKey.AUDITS_MANAGE,
            PermissionKey.DASHBOARD_READ,
            PermissionKey.OVERVIEW_VIEW,
            PermissionKey.LEADS_VIEW,
            PermissionKey.LEADS_CREATE,
            PermissionKey.LEADS_UPDATE,
            PermissionKey.LEADS_DELETE,
            PermissionKey.LEADS_EXPORT,
            PermissionKey.LEADS_IMPORT,
            PermissionKey.LEADS_ASSIGN,
            PermissionKey.LEADS_STATUS,
            PermissionKey.WORKFLOWS_VIEW,
            PermissionKey.WORKFLOWS_CREATE,
            PermissionKey.WORKFLOWS_UPDATE,
            PermissionKey.WORKFLOWS_DELETE,
            PermissionKey.WORKFLOWS_ACTIVATE,
            PermissionKey.AUDITS_VIEW,
            PermissionKey.AUDITS_CREATE,
            PermissionKey.AUDITS_UPDATE,
            PermissionKey.AUDITS_DELETE,
            PermissionKey.AUDITS_EXPORT,
            PermissionKey.TEMPLATES_VIEW,
            PermissionKey.TEMPLATES_CREATE,
            PermissionKey.TEMPLATES_UPDATE,
            PermissionKey.TEMPLATES_DELETE,
        }
    ),
    RoleKey.STAFF: frozenset(
        {
            PermissionKey.TEMPLATES_READ,
            PermissionKey.WORKFLOWS_READ,
            PermissionKey.LEADS_READ,
            PermissionKey.LEADS_ASSIGNED_UPDATE,
            PermissionKey.DASHBOARD_READ,
            PermissionKey.OVERVIEW_VIEW,
            PermissionKey.LEADS_VIEW,
            PermissionKey.LEADS_UPDATE,
            PermissionKey.WORKFLOWS_VIEW,
            PermissionKey.TEMPLATES_VIEW,
        }
    ),
    RoleKey.READONLY: frozenset(
        {
            PermissionKey.TEMPLATES_READ,
            PermissionKey.WORKFLOWS_READ,
            PermissionKey.LEADS_READ,
            PermissionKey.AUDITS_READ,
            PermissionKey.DASHBOARD_READ,
            PermissionKey.OVERVIEW_VIEW,
            PermissionKey.LEADS_VIEW,
            PermissionKey.WORKFLOWS_VIEW,
            PermissionKey.AUDITS_VIEW,
            PermissionKey.TEMPLATES_VIEW,
        }
    ),
}
