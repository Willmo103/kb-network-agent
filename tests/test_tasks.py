import json
import pytest
from pathlib import Path
from kb_network_agent.tasks import validate_task_json, TaskRunner

VALID_TASK = """
{
  "task": {
    "name": "test_task",
    "version": "1.0.0",
    "description": "Test description",
    "target": {
      "type": "host"
    },
    "parameters": {
      "test_param": {
        "type": "string",
        "description": "A test parameter",
        "required": true
      }
    },
    "actions": [
      {
        "type": "command",
        "command": "echo <test_param>"
      }
    ]
  }
}
"""

INVALID_TASK_NAME = """
{
  "task": {
    "name": "TEST-TASK-BAD-NAME",
    "version": "1.0.0",
    "description": "Test",
    "target": {
      "type": "host"
    },
    "actions": []
  }
}
"""


def test_valid_task_validation():
    valid, errors = validate_task_json(VALID_TASK)
    assert valid is True
    assert len(errors) == 0


def test_invalid_task_validation():
    # 1. Invalid name (should be snake_case)
    valid, errors = validate_task_json(INVALID_TASK_NAME)
    assert valid is False
    assert any("task -> name" in err for err in errors)


def test_parameter_substitution():
    runner = TaskRunner()
    text = "Hello <name>, you are {age} years old."
    params = {"name": "Will", "age": 25}
    substituted = runner._substitute_params(text, params)
    assert substituted == "Hello Will, you are 25 years old."
