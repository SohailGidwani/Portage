"""Sandbox — ephemeral, network-off Docker execution adapter + test-report parsing."""

from .docker import DockerSandbox
from .report import TestCaseResult, TestReport, parse_junit_xml

__all__ = ["DockerSandbox", "TestReport", "TestCaseResult", "parse_junit_xml"]
