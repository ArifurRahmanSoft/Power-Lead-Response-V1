from dataclasses import dataclass


@dataclass(frozen=True)
class MenuDefinition:
    key: str
    label: str
    actions: tuple[str, ...]


MENU_CATALOG: tuple[MenuDefinition, ...] = (
    MenuDefinition("overview", "Overview", ("view",)),
    MenuDefinition(
        "leads",
        "Leads",
        ("view", "create", "update", "delete", "export", "import", "assign", "status"),
    ),
    MenuDefinition(
        "workflows",
        "Workflows",
        ("view", "create", "update", "delete", "activate", "approve"),
    ),
    MenuDefinition("audits", "Audits", ("view", "create", "update", "delete", "export")),
    MenuDefinition(
        "templates",
        "Templates",
        ("view", "create", "update", "delete", "approve"),
    ),
    MenuDefinition(
        "settings.roles",
        "Role Setup",
        ("view", "create", "update", "delete"),
    ),
    MenuDefinition(
        "settings.users",
        "User Setup",
        ("view", "create", "update", "delete", "reset_password"),
    ),
    MenuDefinition(
        "settings.menu_permissions",
        "Menu Permission",
        ("view", "update"),
    ),
    MenuDefinition(
        "settings.integrations",
        "Integration Settings",
        ("view", "update"),
    ),
)

MENU_PERMISSION_KEYS = frozenset(
    f"{menu.key}.{action}"
    for menu in MENU_CATALOG
    for action in menu.actions
)
PRIVILEGED_MANAGEMENT_KEYS = frozenset(
    key for key in MENU_PERMISSION_KEYS if key.startswith("settings.")
)
OWNER_REQUIRED_MANAGEMENT_KEYS = frozenset(
    key
    for key in MENU_PERMISSION_KEYS
    if key.startswith("settings.roles.")
    or key.startswith("settings.users.")
    or key.startswith("settings.menu_permissions.")
)
