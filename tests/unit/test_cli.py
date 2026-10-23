from app.cli import STAFF, STAFF_DOMAIN, staff_email


def test_sample_staff():
    emails = [email for email, _, _ in STAFF]
    assert len(STAFF) == 53 and len(set(emails)) == 53
    assert all(e.endswith(f"@{STAFF_DOMAIN}") and e == e.lower() and " " not in e for e in emails)
    assert staff_email("James O'Connor") == "james.oconnor@jigsaw-insurance.com"
