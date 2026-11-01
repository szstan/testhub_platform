"""Portable expressions for exact comparisons of canonical integer identifiers."""
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import CharField
from django.db.models.functions import Cast, Collate


def canonical_id_text(expression='pk', *, using=DEFAULT_DB_ALIAS):
    """Cast an integer ID without inheriting a conflicting MySQL collation.

    Identifier strings are canonical ASCII decimal values. An explicit binary
    collation preserves exact identity when the connection and a legacy target
    column use different utf8mb4 collations. Other vendors retain their native
    text cast and are never asked to resolve a MySQL-only collation name.
    """
    result = Cast(expression, output_field=CharField())
    if connections[using].vendor == 'mysql':
        return Collate(result, 'utf8mb4_bin')
    return result
