"""Pure delivery policies for verified workflow candidates."""


def _has_written_result(results) -> bool:
    return any(result.status == "written" for result in results)


def can_publish_candidate(verification, results) -> bool:
    """Legacy fixed-template gate; preserve its existing layout states."""
    return (
        verification.content_ok
        and verification.layout_check != "needs_review"
        and _has_written_result(results)
    )


def can_publish_general_candidate(verification, results) -> bool:
    """General workflow requires a positive layout verification result."""
    return (
        verification.content_ok
        and verification.layout_check == "passed"
        and _has_written_result(results)
    )


def workflow_outcome(verification, report_rows) -> str:
    if not verification.content_ok:
        return "failed"
    if verification.needs_review or any(row["status"] != "written" for row in report_rows):
        return "partial"
    return "complete"


def general_workflow_outcome(verification, published_file, processing_warnings, report_rows) -> str:
    if not verification.content_ok:
        return "failed"
    if not published_file or processing_warnings or any(row["status"] != "written" for row in report_rows):
        return "partial"
    return "complete"
