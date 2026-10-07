import argparse
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.core.database import SessionLocal
from app.models.lead import LeadImportBatch, LeadImportRow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete expired lead-import row snapshots while retaining batch evidence.",
    )
    parser.add_argument("--limit", type=int, default=1000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.limit <= 10000:
        raise SystemExit("Limit must be between 1 and 10000")
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as database_session:
        batch_ids = list(
            database_session.scalars(
                select(LeadImportBatch.id)
                .where(
                    LeadImportBatch.expires_at <= now,
                    LeadImportBatch.status != "committing",
                )
                .order_by(LeadImportBatch.expires_at)
                .limit(args.limit)
            ).all()
        )
        if batch_ids:
            database_session.execute(
                delete(LeadImportRow).where(LeadImportRow.batch_id.in_(batch_ids))
            )
            for batch in database_session.scalars(
                select(LeadImportBatch).where(LeadImportBatch.id.in_(batch_ids))
            ):
                if batch.status == "previewed":
                    batch.status = "expired"
    print(f"Cleaned snapshots for {len(batch_ids)} expired import batches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
