"""Count saved APP element references without exposing inaccessible project data."""
from __future__ import annotations

import re
from collections import Counter

from .access import accessible_projects, is_app_admin
from .models import AppComponent, AppCustomComponent, AppProjectComponent, AppTestCase


_INTEGER_ID = re.compile(r"^[0-9]+$")
_ELEMENT_REFERENCE_FIELDS = frozenset({
    "element_id", "fallback_element_id", "start_element_id", "end_element_id",
    "target_element_id", "click_element_id", "ocr_element_id",
})


def _scan_reference_payload(payload):
    """Return literal element IDs and whether a selector ID cannot be resolved."""
    references = Counter()
    unknown = False
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _ELEMENT_REFERENCE_FIELDS:
                    if type(child) is int:
                        if child > 0:
                            references[child] += 1
                    elif type(child) is bool:
                        if child:
                            unknown = True
                    elif isinstance(child, str):
                        normalized = child.strip()
                        if normalized and _INTEGER_ID.fullmatch(normalized):
                            element_id = int(normalized)
                            if element_id > 0:
                                references[element_id] += 1
                        elif normalized:
                            unknown = True
                    elif child is not None:
                        unknown = True
                if isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(value, list):
            pending.extend(item for item in value if isinstance(item, (dict, list)))
    return references, unknown


def _iter_reference_documents(user):
    admin = is_app_admin(user)
    project_ids = set(accessible_projects(user).values_list("pk", flat=True))
    user_id = getattr(user, "pk", None)

    for case in AppTestCase.objects.only("project_id", "created_by_id", "ui_flow").iterator():
        visible = admin or case.project_id in project_ids or case.created_by_id == user_id
        yield case.ui_flow, visible

    for component in AppProjectComponent.objects.only(
        "project_id", "steps", "default_config"
    ).iterator():
        visible = admin or component.project_id in project_ids
        yield component.steps, visible
        yield component.default_config, visible

    # These are global component catalogs exposed to all authenticated users.
    for component in AppComponent.objects.only("default_config").iterator():
        yield component.default_config, True

    for component in AppCustomComponent.objects.only("steps", "default_config").iterator():
        yield component.steps, True
        yield component.default_config, True


def reference_summary_for_ids(elements, user):
    """Summarize saved references without leaking hidden-project counts.

    ``reference_count`` includes only accessible cases/components and global
    component definitions. Unresolved IDs remain a separate lower-bound flag.
    ``is_referenced`` includes hidden references and is for archive protection
    only; callers must not expose it or hidden-project counts.
    """
    elements = list(elements)
    if not elements:
        return {}
    element_ids = {element.pk for element in elements}
    visible_counts = Counter()
    all_counts = Counter()
    visible_unknown = False
    all_unknown = False

    for payload, visible in _iter_reference_documents(user):
        counts, unknown = _scan_reference_payload(payload)
        if counts:
            all_counts.update({key: value for key, value in counts.items() if key in element_ids})
            if visible:
                visible_counts.update({key: value for key, value in counts.items() if key in element_ids})
        if unknown:
            all_unknown = True
            if visible:
                visible_unknown = True

    return {
        element_id: {
            "reference_count": visible_counts[element_id],
            "reference_count_unknown": visible_unknown,
            "is_referenced": bool(all_counts[element_id] or all_unknown),
        }
        for element_id in element_ids
    }


def validate_element_ids(raw_ids):
    """Validate and normalize a bounded bulk archive request."""
    if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 100:
        return None
    normalized = []
    for value in raw_ids:
        if type(value) is int and value > 0:
            normalized.append(value)
        elif isinstance(value, str) and _INTEGER_ID.fullmatch(value):
            normalized.append(int(value))
        else:
            return None
    if len(set(normalized)) != len(normalized):
        return None
    return normalized
