"""Identity normalization and collision detection shared by models, serializers,
management commands and migrations. Pure functions: no model imports."""
from collections import defaultdict


def normalize_email(value):
    """Trim and lowercase the complete address (case-insensitive identity)."""
    if value is None:
        return None
    return str(value).strip().lower()


def normalize_matric(value):
    """Trim and uppercase; blank becomes NULL so blanks never collide."""
    if value is None:
        return None
    value = str(value).strip().upper()
    return value or None


def normalize_department(value):
    return (value or '').strip()


def find_identity_collisions(rows):
    """Return {'email': [[ids...], ...], 'matric_number': [[ids...], ...]}.

    ``rows`` is an iterable of (id, email, matric_number). Only user IDs are
    reported: normalized values are personal data and are never returned.
    """
    groups = {'email': defaultdict(list), 'matric_number': defaultdict(list)}
    for user_id, email, matric in rows:
        # Blank/whitespace/NULL emails all normalize to '' and collide with
        # each other under the unique constraints, so '' is a real key.
        groups['email'][normalize_email(email) or ''].append(user_id)
        matric_key = normalize_matric(matric)
        if matric_key:
            groups['matric_number'][matric_key].append(user_id)
    return {
        field: sorted(sorted(ids) for ids in values.values() if len(ids) > 1)
        for field, values in groups.items()
    }


def has_collisions(report):
    return any(report.values())


def format_collisions(report):
    lines = []
    for field, groups in report.items():
        for ids in groups:
            lines.append(f'{field}: user IDs {", ".join(str(i) for i in ids)}')
    return lines
