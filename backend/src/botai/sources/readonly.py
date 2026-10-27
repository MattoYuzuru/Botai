"""What the code allows beyond GET (security.md §5: reads only by default).

A recipe may POST only to an endpoint that is read-only by construction. The allowlist lives in
code, not in the spec: the spec merely declares `readOnlyPost: true`, the lint and the runtime
both check this table.
"""

from __future__ import annotations

from collections.abc import Mapping

# path -> (query parameter naming the called method, allowed method names). AJAX-style gateways
# of an LMS multiplex many methods behind one path; only the listed read methods pass.
_MOODLE_AJAX_READS = frozenset(
    {
        "core_calendar_get_action_events_by_timesort",
        "core_course_get_enrolled_courses_by_timeline_classification",
    }
)
READ_ONLY_POST: dict[str, tuple[str, frozenset[str]]] = {
    "/lib/ajax/service.php": ("info", _MOODLE_AJAX_READS),
}


def is_read_only_post(path: str, query: Mapping[str, str] | None) -> bool:
    """True only for a POST that the code knows cannot change anything on the source."""
    base = path.split("?", 1)[0]
    entry = READ_ONLY_POST.get(base)
    if entry is None or not query:
        return False
    param, allowed = entry
    return query.get(param) in allowed
