"""TEMPORARY -- M0 task 1 only.

This test exists to prove the GitHub Actions workflow can report a red build.
It is deleted in the very next commit. A green CI that has never gone red is
indistinguishable from a CI that never ran (AUDIT.md P4 / REBUILD_PLAN M0).
"""


def test_ci_can_report_a_failure():
    assert 1 == 2, "deliberate failure: proving the CI pipeline can go red"
