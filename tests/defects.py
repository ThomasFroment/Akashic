"""Known defects from the 2026-11-10 audits, pinned as strict expected failures.

Each test marked with ``known_defect`` asserts the *correct* behaviour and fails today. The
marker is ``xfail(strict=True)``, so the run stays green while the defect exists and turns red
(XPASS) the moment a change fixes it: then delete the marker and keep the test as a regression
guard. ``pytest -rx`` lists every open defect, which is the baseline count before any fix.

``raises`` pins the failure mode (an assertion by default), so a test that breaks for an
unrelated reason (an import error, a renamed helper) is reported as a failure, not as the
defect. Finding numbers refer to ``docs/audits/2026-11-10.md`` (#1-#112) and
``docs/audits/2026-11-10-2.md`` (#113-#186).
"""

from __future__ import annotations

import pytest

AUDITS = "docs/audits/2026-11-10.md, docs/audits/2026-11-10-2.md"


def known_defect(
    number: int,
    summary: str,
    raises: type[BaseException] | tuple[type[BaseException], ...] = AssertionError,
) -> pytest.MarkDecorator:
    """Mark a test that asserts the fixed behaviour of audit finding ``number``."""
    return pytest.mark.xfail(strict=True, raises=raises, reason=f"audit #{number}: {summary}")
