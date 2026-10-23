"""Command line tasks.

python -m app.cli migrate                  apply database migrations
python -m app.cli worker                   run the ingestion worker
python -m app.cli seed [--wait]            load the Jigsaw sample catalogue, staff accounts and documents
python -m app.cli create-user EMAIL "Full Name" ROLE
python -m app.cli ingest                   process queued ingestion jobs now, then exit
python -m app.cli retry-failed             queue every failed document again (e.g. after fixing the API key)
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from datetime import date

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from app.config import PROJECT_ROOT, ROLES, get_settings
from app.db import session_scope
from app.log import configure_logging
from app.models import Document, Product, User
from app.security import hash_password, password_problems

SAMPLE_DATA = PROJECT_ROOT / "sample_data"

STAFF_DOMAIN = "jigsaw-insurance.com"
# Sample staff, 53 in all. The first name in each role is the account used in the README and the tests.
STAFF_BY_ROLE = {
    "admin": ["Priya Shah", "Oliver Bennett", "Siobhan Kelly", "Rahul Mehta"],
    "underwriter": [
        "Tom Whitfield", "Emily Carter", "James O'Connor", "Fatima Hussain", "George Thompson",
        "Niamh Byrne", "Arjun Nair", "Charlotte Hughes", "Liam Doyle", "Grace Edwards", "Mohammed Rahman",
        "Hannah Lewis", "Conor Walsh", "Sophie Turner", "Kwame Mensah", "Isla Robertson", "Ben Clarke",
    ],
    "claims_handler": [
        "Aisha Okafor", "Chloe Morgan", "Ryan Fitzgerald", "Zara Ahmed", "Matthew Price", "Aoife Brennan",
        "Samuel Okoro", "Lucy Harrison", "Patrick Quinn", "Amelia Ward", "Hassan Ali", "Megan Phillips",
        "Callum Fraser", "Ella Robinson", "Jack Kennedy", "Nadia Kowalski",
    ],
    "customer_service": [
        "Daniel Murphy", "Olivia Davies", "Sean Murray", "Leah Green", "Joshua Wright", "Ciara Nolan",
        "Adam Hall", "Mia Jackson", "Eoin Gallagher", "Ruby Scott", "Tariq Malik", "Holly Baker",
        "Declan Ryan", "Jessica Wood", "Ravi Patel", "Freya Campbell",
    ],
}  # fmt: skip


def staff_email(full_name: str) -> str:
    """'James O'Connor' -> james.oconnor@jigsaw-insurance.com"""
    local_part = full_name.lower().replace("'", "").replace(" ", ".")
    return f"{local_part}@{STAFF_DOMAIN}"


STAFF = [(staff_email(name), name, role) for role, names in STAFF_BY_ROLE.items() for name in names]


def migrate() -> None:
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")


def create_user(email: str, full_name: str, role: str, password: str) -> bool:
    """Create a user unless the email exists. Returns True if created."""
    if role not in ROLES:
        raise SystemExit(f"Role must be one of: {', '.join(ROLES)}")
    if problems := password_problems(password):
        raise SystemExit(" ".join(problems))
    with session_scope() as s:
        if s.execute(select(User).where(func.lower(User.email) == email.lower())).first():
            return False
        s.add(
            User(email=email.lower(), full_name=full_name, role=role, password_hash=hash_password(password))
        )
    return True


def retry_failed() -> int:
    with session_scope() as s:
        s.execute(
            text("UPDATE documents SET status = 'queued', error_message = NULL WHERE status = 'failed'")
        )
        return s.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'queued', attempts = 0, run_after = now(), error_message = NULL, "
                "finished_at = NULL WHERE status = 'failed' AND document_id IN "
                "(SELECT id FROM documents WHERE status = 'queued')"
            )
        ).rowcount


def seed(wait: bool) -> None:
    from app.ingestion.worker import drain
    from app.services.documents import UploadDetails, create_document

    manifest = json.loads((SAMPLE_DATA / "manifest.json").read_text(encoding="utf-8"))
    settings = get_settings()
    password = (
        settings.seed_password.get_secret_value() if settings.seed_password else secrets.token_urlsafe(12)
    )
    created = [email for email, name, role in STAFF if create_user(email, name, role, password)]
    if created:
        print(
            f"{len(created)} staff account(s) created (@{STAFF_DOMAIN}, {len(STAFF) - len(created)} already present)."
        )
        if not settings.seed_password:
            print(f"Password for all of them (shown once): {password}")

    with session_scope() as s:
        for item in manifest:
            if s.execute(select(Product).where(Product.code == item["product_code"])).first() is None:
                s.add(
                    Product(
                        code=item["product_code"],
                        name=item["product_name"],
                        line_of_business=item["line_of_business"],
                    )
                )
    keys: dict[str, str] = {}
    queued = 0
    # Wordings first, so endorsements can be attached to them.
    for item in sorted(manifest, key=lambda i: i["kind"] != "wording"):
        with session_scope() as s:
            existing = s.execute(
                select(Document).where(Document.sha256 == item["sha256"], Document.status != "deleted")
            ).scalar_one_or_none()
            if existing is not None:
                keys[item["key"]] = existing.id
                continue
            details = UploadDetails(
                product_code=item["product_code"],
                kind=item["kind"],
                title=item["title"],
                version_label=item["version"],
                region=item["region"],
                effective_from=date.fromisoformat(item["effective_from"]),
                effective_to=date.fromisoformat(item["effective_to"]) if item["effective_to"] else None,
                applies_to_id=keys.get(item["applies_to"]) if item["applies_to"] else None,
                allowed_roles=item["allowed_roles"],
                reference=item["reference"],
            )
            with (SAMPLE_DATA / "pdfs" / item["filename"]).open("rb") as f:
                doc = create_document(s, f, item["filename"], details, uploaded_by=None)
            keys[item["key"]] = doc.id
            queued += 1
    print(f"{queued} document(s) queued for ingestion ({len(manifest) - queued} already present).")
    if wait and queued:
        print("Ingesting now...", drain())


def main(argv: list[str] | None = None) -> None:
    configure_logging(get_settings().log_level)
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("worker")
    sub.add_parser("ingest")
    sub.add_parser("retry-failed")
    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--wait", action="store_true", help="ingest the documents before exiting")
    p_user = sub.add_parser("create-user")
    p_user.add_argument("email")
    p_user.add_argument("full_name")
    p_user.add_argument("role", choices=ROLES)
    args = parser.parse_args(argv)

    if args.command == "migrate":
        migrate()
    elif args.command == "worker":
        from app.ingestion.worker import run_worker

        run_worker()
    elif args.command == "ingest":
        from app.ingestion.worker import drain

        print(drain())
    elif args.command == "retry-failed":
        print(f"{retry_failed()} document(s) queued again.")
    elif args.command == "seed":
        seed(args.wait)
    elif args.command == "create-user":
        password = getpass.getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
        print(
            "created"
            if create_user(args.email, args.full_name, args.role, password)
            else "email already exists"
        )


if __name__ == "__main__":
    main()
