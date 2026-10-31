"""Canonical identities shared by quality projections."""


def effective_environment(item_environment, run_environment):
    value = str(item_environment or '')
    if value in {'', 'unknown'}:
        value = str(run_environment or 'unknown')
    return value or 'unknown'
