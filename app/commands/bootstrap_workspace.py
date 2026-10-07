import argparse

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.bootstrap_service import (
    BootstrapPrerequisiteError,
    BootstrapUserNotFoundError,
    InvalidWorkspaceNameError,
    InvalidWorkspaceSlugError,
    bootstrap_owner_workspace,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Locally create or reuse a workspace and assign an existing user as owner.",
    )
    parser.add_argument(
        "--identifier",
        required=True,
        help="Exact existing email or login ID (display names are not matched)",
    )
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument(
        "--workspace-slug",
        help="Optional stable slug; derived from workspace name when omitted",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if settings.live:
        raise SystemExit("Workspace bootstrap is disabled when LIVE=true")

    try:
        with SessionLocal.begin() as database_session:
            result = bootstrap_owner_workspace(
                database_session,
                identifier=args.identifier,
                workspace_name=args.workspace_name,
                workspace_slug=args.workspace_slug,
            )
    except BootstrapUserNotFoundError as exception:
        raise SystemExit("No user matched the supplied email or login ID") from exception
    except (
        BootstrapPrerequisiteError,
        InvalidWorkspaceNameError,
        InvalidWorkspaceSlugError,
    ) as exception:
        raise SystemExit(str(exception)) from exception
    except IntegrityError as exception:
        raise SystemExit(
            "Workspace bootstrap failed because of conflicting data; retry the command"
        ) from exception

    action = "Created" if result.created_tenant else "Reused"
    membership_action = "created" if result.created_membership else "verified"
    print(
        f"{action} workspace '{result.tenant.slug}'; owner membership "
        f"{membership_action} for login ID '{result.user.login_id}'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
