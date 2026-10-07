"""add tenant settings administration

Revision ID: dd001d335ac3
Revises: 4200410ff950
Create Date: 2026-10-01 16:33:42.663977
"""
from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = 'dd001d335ac3'
down_revision: str | Sequence[str] | None = '4200410ff950'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_IDS = {
    "owner": uuid.UUID("10000000-0000-0000-0000-000000000001"),
    "operator": uuid.UUID("10000000-0000-0000-0000-000000000002"),
    "staff": uuid.UUID("10000000-0000-0000-0000-000000000003"),
    "readonly": uuid.UUID("10000000-0000-0000-0000-000000000004"),
}
NEW_PERMISSION_KEYS = [
    "audits.create", "audits.delete", "audits.export", "audits.update", "audits.view",
    "leads.create", "leads.delete", "leads.export", "leads.update", "leads.view",
    "overview.view", "settings.integrations.update", "settings.integrations.view",
    "settings.menu_permissions.update", "settings.menu_permissions.view",
    "settings.roles.create", "settings.roles.delete", "settings.roles.update",
    "settings.roles.view", "settings.users.create", "settings.users.delete",
    "settings.users.reset_password", "settings.users.update", "settings.users.view",
    "templates.create", "templates.delete", "templates.update", "templates.view",
    "workflows.activate", "workflows.create", "workflows.delete", "workflows.update",
    "workflows.view",
]
PERMISSION_IDS = {
    key: uuid.UUID(f"30000000-0000-0000-0000-{index:012d}")
    for index, key in enumerate(NEW_PERMISSION_KEYS, start=1)
}
ROLE_PERMISSION_KEYS = {
    "owner": set(NEW_PERMISSION_KEYS),
    "operator": {
        "overview.view", "leads.view", "leads.create", "leads.update",
        "leads.delete", "leads.export", "workflows.view", "workflows.create",
        "workflows.update", "workflows.delete", "workflows.activate", "audits.view",
        "audits.create", "audits.update", "audits.delete", "audits.export",
        "templates.view", "templates.create", "templates.update", "templates.delete",
    },
    "staff": {
        "overview.view", "leads.view", "leads.update", "workflows.view",
        "templates.view",
    },
    "readonly": {
        "overview.view", "leads.view", "workflows.view", "audits.view",
        "templates.view",
    },
}


def upgrade() -> None:
    op.add_column('t_role', sa.Column('tenant_id', sa.Uuid(), nullable=True))
    op.add_column('t_role', sa.Column('normalized_name', sa.String(length=100), nullable=True))
    op.add_column('t_role', sa.Column('is_system', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.execute("UPDATE t_role SET normalized_name = lower(name), is_system = true")
    op.alter_column('t_role', 'normalized_name', nullable=False)
    op.alter_column('t_role', 'key',
               existing_type=sa.VARCHAR(length=30),
               type_=sa.String(length=80),
               existing_nullable=False)
    op.alter_column('t_role', 'name',
               existing_type=sa.VARCHAR(length=80),
               type_=sa.String(length=100),
               existing_nullable=False)
    op.create_unique_constraint('uq_t_role_tenant_normalized_name', 't_role', ['tenant_id', 'normalized_name'])
    op.create_foreign_key(
        'fk_t_role_tenant_id_t_tenant',
        't_role',
        't_tenant',
        ['tenant_id'],
        ['id'],
        ondelete='CASCADE',
    )

    permission_table = sa.table(
        't_permission',
        sa.column('id', sa.Uuid()),
        sa.column('key', sa.String()),
        sa.column('description', sa.String()),
    )
    role_permission_table = sa.table(
        't_role_permission',
        sa.column('role_id', sa.Uuid()),
        sa.column('permission_id', sa.Uuid()),
    )
    op.bulk_insert(
        permission_table,
        [
            {
                'id': PERMISSION_IDS[key],
                'key': key,
                'description': key.replace('.', ' ').replace('_', ' ').title(),
            }
            for key in NEW_PERMISSION_KEYS
        ],
    )
    op.bulk_insert(
        role_permission_table,
        [
            {
                'role_id': ROLE_IDS[role_key],
                'permission_id': PERMISSION_IDS[permission_key],
            }
            for role_key, permission_keys in ROLE_PERMISSION_KEYS.items()
            for permission_key in sorted(permission_keys)
        ],
    )


def downgrade() -> None:
    role_permission_table = sa.table(
        't_role_permission',
        sa.column('permission_id', sa.Uuid()),
    )
    permission_table = sa.table(
        't_permission',
        sa.column('id', sa.Uuid()),
    )
    op.execute(
        role_permission_table.delete().where(
            role_permission_table.c.permission_id.in_(PERMISSION_IDS.values())
        )
    )
    op.execute(
        permission_table.delete().where(
            permission_table.c.id.in_(PERMISSION_IDS.values())
        )
    )
    op.drop_constraint('fk_t_role_tenant_id_t_tenant', 't_role', type_='foreignkey')
    op.drop_constraint('uq_t_role_tenant_normalized_name', 't_role', type_='unique')
    op.alter_column('t_role', 'name',
               existing_type=sa.String(length=100),
               type_=sa.VARCHAR(length=80),
               existing_nullable=False)
    op.alter_column('t_role', 'key',
               existing_type=sa.String(length=80),
               type_=sa.VARCHAR(length=30),
               existing_nullable=False)
    op.drop_column('t_role', 'is_system')
    op.drop_column('t_role', 'normalized_name')
    op.drop_column('t_role', 'tenant_id')

