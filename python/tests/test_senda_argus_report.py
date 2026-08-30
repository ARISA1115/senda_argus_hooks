"""senda_argus_report の抽出・emit ロジックのテスト。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from senda_argus_hooks.instrumentors.argus_sdk import _extract_senda_argus_report


def _tool_call(name: str, arguments: Any) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


class TestExtractSendaArgusReport:
    def test_no_tool_calls_returns_none(self):
        response = {"message": {"content": "hello"}}
        report, actual = _extract_senda_argus_report(response)
        assert report is None
        assert actual == []

    def test_tool_calls_without_senda_argus_report(self):
        response = {
            "message": {
                "tool_calls": [_tool_call("select_department", {"department": "security"})]
            }
        }
        report, actual = _extract_senda_argus_report(response)
        assert report is None
        assert len(actual) == 1

    def test_extracts_senda_argus_report_dict_arguments(self):
        report_data = {
            "event_type": "llm.tool_selection.proposed",
            "tool_name": "select_department",
            "confidence": "high",
            "uncertainty": "low",
            "alternatives_considered": [],
        }
        response = {
            "message": {
                "tool_calls": [
                    _tool_call("select_department", {"department": "security"}),
                    _tool_call("senda_argus_report", report_data),
                ]
            }
        }
        report, actual = _extract_senda_argus_report(response)
        assert report is not None
        assert report["tool_name"] == "select_department"
        assert report["confidence"] == "high"
        assert len(actual) == 1
        assert actual[0]["function"]["name"] == "select_department"

    def test_senda_argus_report_removed_from_tool_calls(self):
        report_data = {"tool_name": "do_thing"}
        response = {
            "message": {
                "tool_calls": [
                    _tool_call("do_thing", {}),
                    _tool_call("senda_argus_report", report_data),
                ]
            }
        }
        _extract_senda_argus_report(response)
        remaining = response["message"]["tool_calls"]
        assert len(remaining) == 1
        assert remaining[0]["function"]["name"] == "do_thing"

    def test_string_arguments_parsed_as_json(self):
        report_data = {"tool_name": "search", "confidence": "medium"}
        response = {
            "message": {
                "tool_calls": [
                    _tool_call("search", "{}"),
                    _tool_call("senda_argus_report", json.dumps(report_data)),
                ]
            }
        }
        report, _ = _extract_senda_argus_report(response)
        assert report is not None
        assert report["tool_name"] == "search"

    def test_only_senda_argus_report_no_other_tools(self):
        report_data = {"tool_name": "x", "confidence": "low"}
        response = {
            "message": {
                "tool_calls": [_tool_call("senda_argus_report", report_data)]
            }
        }
        report, actual = _extract_senda_argus_report(response)
        assert report is not None
        assert actual == []

    def test_non_dict_message_returns_none(self):
        assert _extract_senda_argus_report({"message": None}) == (None, [])
        assert _extract_senda_argus_report({}) == (None, [])

    def test_steering_detected_when_tool_name_mismatches(self):
        """senda_argus_report.tool_name が実際のツール名と異なる場合。"""
        report_data = {"tool_name": "expected_tool"}
        response = {
            "message": {
                "tool_calls": [
                    _tool_call("actual_different_tool", {}),
                    _tool_call("senda_argus_report", report_data),
                ]
            }
        }
        report, actual = _extract_senda_argus_report(response)
        actual_names = {(tc.get("function") or {}).get("name") for tc in actual}
        steering = bool(actual) and report.get("tool_name") not in actual_names
        assert steering is True

    def test_no_steering_when_tool_name_matches(self):
        report_data = {"tool_name": "correct_tool"}
        response = {
            "message": {
                "tool_calls": [
                    _tool_call("correct_tool", {}),
                    _tool_call("senda_argus_report", report_data),
                ]
            }
        }
        report, actual = _extract_senda_argus_report(response)
        actual_names = {(tc.get("function") or {}).get("name") for tc in actual}
        steering = bool(actual) and report.get("tool_name") not in actual_names
        assert steering is False
