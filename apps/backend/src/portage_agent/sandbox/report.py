"""Parse the sandbox's JUnit XML into our own structured TestReport.

JUnit XML is pytest's built-in machine-readable format (stable across versions). We own the
TestReport shape so downstream (Report node, dashboard, eval) doesn't depend on JUnit quirks.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class TestCaseResult:
    name: str
    classname: str
    outcome: str  # passed | failed | error | skipped
    time: float = 0.0


@dataclass(slots=True)
class TestReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    cases: list[TestCaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errors == 0 and self.total > 0

    def to_dict(self) -> dict:
        return asdict(self)


def parse_junit_xml(xml_text: str) -> TestReport:
    """Parse JUnit XML text into a TestReport. Tolerates <testsuites> or a bare <testsuite>."""
    root = ET.fromstring(xml_text)
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    report = TestReport()
    for suite in suites:
        report.duration_seconds += float(suite.get("time", 0) or 0)
        for case in suite.findall("testcase"):
            if case.find("error") is not None:
                outcome = "error"
            elif case.find("failure") is not None:
                outcome = "failed"
            elif case.find("skipped") is not None:
                outcome = "skipped"
            else:
                outcome = "passed"
            report.cases.append(
                TestCaseResult(
                    name=case.get("name", "?"),
                    classname=case.get("classname", ""),
                    outcome=outcome,
                    time=float(case.get("time", 0) or 0),
                )
            )

    report.total = len(report.cases)
    report.passed = sum(c.outcome == "passed" for c in report.cases)
    report.failed = sum(c.outcome == "failed" for c in report.cases)
    report.errors = sum(c.outcome == "error" for c in report.cases)
    report.skipped = sum(c.outcome == "skipped" for c in report.cases)
    report.duration_seconds = round(report.duration_seconds, 3)
    return report
