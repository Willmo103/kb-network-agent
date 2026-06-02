import os
import sys
import re
import json
import shutil
import tempfile
import subprocess
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from pydantic import BaseModel, Field, field_validator, ValidationError

logger = logging.getLogger("kb-network-agent")

# Directories
KB_ROOT = Path.home() / ".kb"
TASKS_DIR = KB_ROOT / "tasks"

# ----------------------------------------------------
# Pydantic Schema definitions for programmatic validation
# ----------------------------------------------------

class TargetSpec(BaseModel):
    type: str  # host, service, os, all
    criteria: Optional[Dict[str, Any]] = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        allowed = ["host", "service", "os", "all"]
        if v not in allowed:
            raise ValueError(f"target.type must be one of {allowed}")
        return v

class ParameterSpec(BaseModel):
    type: str  # string, number, boolean, array, object
    description: Optional[str] = None
    required: Optional[bool] = False
    default: Optional[Any] = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        allowed = ["string", "number", "boolean", "array", "object"]
        if v not in allowed:
            raise ValueError(f"parameter type must be one of {allowed}")
        return v

class ActionSpec(BaseModel):
    type: str  # script, command, api_call
    command: Optional[str] = None
    interpreter: Optional[str] = None  # bash, python3, powershell, python
    parameters: Optional[Dict[str, str]] = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        allowed = ["script", "command", "api_call"]
        if v not in allowed:
            raise ValueError(f"action.type must be one of {allowed}")
        return v

    @field_validator("interpreter")
    @classmethod
    def validate_interpreter(cls, v):
        if v is not None:
            allowed = ["bash", "python3", "powershell", "python"]
            if v not in allowed:
                raise ValueError(f"action.interpreter must be one of {allowed}")
        return v

class ScriptSpec(BaseModel):
    script: Optional[str] = None
    interpreter: Optional[str] = None
    parameters: Optional[Dict[str, str]] = None

    @field_validator("interpreter")
    @classmethod
    def validate_interpreter(cls, v):
        if v is not None:
            allowed = ["bash", "python3", "powershell", "python"]
            if v not in allowed:
                raise ValueError(f"interpreter must be one of {allowed}")
        return v

class TaskSpec(BaseModel):
    name: str
    version: str
    description: str
    target: TargetSpec
    parameters: Optional[Dict[str, ParameterSpec]] = None
    actions: List[ActionSpec]
    validation: Optional[ScriptSpec] = None
    rollback: Optional[ScriptSpec] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not re.match(r"^[a-z0-9_]+$", v):
            raise ValueError("task.name must be snake_case (lowercase letters, numbers, and underscores)")
        return v

class TaskEnvelope(BaseModel):
    task: TaskSpec


def validate_task_json(json_str: str) -> Tuple[bool, List[str]]:
    """Programmatically validates a task JSON string against the schema.
    No AI/LLM involvement for safety.
    """
    try:
        TaskEnvelope.model_validate_json(json_str)
        return True, []
    except ValidationError as e:
        errors = []
        for error in e.errors():
            loc_str = " -> ".join(str(loc) for loc in error["loc"])
            errors.append(f"[{loc_str}]: {error['msg']} (input: {error.get('input')})")
        return False, errors
    except json.JSONDecodeError as jde:
        return False, [f"Invalid JSON syntax: {jde.msg} at line {jde.lineno} col {jde.colno}"]
    except Exception as ex:
        return False, [f"Unknown validation error: {str(ex)}"]


# ----------------------------------------------------
# Task Runner implementation
# ----------------------------------------------------

class TaskRunner:
    def __init__(self, tasks_dir: Path = TASKS_DIR):
        self.tasks_dir = tasks_dir
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def load_task(self, name: str) -> Optional[TaskSpec]:
        task_file = self.tasks_dir / f"{name}.json"
        if not task_file.exists():
            return None
        try:
            with open(task_file, "r", encoding="utf-8") as f:
                envelope = TaskEnvelope.model_validate_json(f.read())
                return envelope.task
        except Exception as e:
            logger.error(f"Failed to load task {name}: {e}")
            return None

    def list_tasks(self) -> List[Dict[str, Any]]:
        tasks = []
        for f in self.tasks_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as file:
                    envelope = TaskEnvelope.model_validate_json(file.read())
                    tasks.append({
                        "name": envelope.task.name,
                        "version": envelope.task.version,
                        "description": envelope.task.description,
                        "target": envelope.task.target.model_dump()
                    })
            except Exception:
                continue
        return tasks

    def import_task(self, json_str: str) -> Tuple[bool, List[str]]:
        valid, errors = validate_task_json(json_str)
        if not valid:
            return False, errors

        try:
            envelope = TaskEnvelope.model_validate_json(json_str)
            target_path = self.tasks_dir / f"{envelope.task.name}.json"
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(json.loads(json_str), indent=2))
            return True, [f"Task '{envelope.task.name}' successfully imported."]
        except Exception as e:
            return False, [f"Failed to write task file: {e}"]

    def remove_task(self, name: str) -> bool:
        task_file = self.tasks_dir / f"{name}.json"
        if task_file.exists():
            task_file.unlink()
            return True
        return False

    def _substitute_params(self, text: str, params: Dict[str, Any]) -> str:
        if not text:
            return text
        # Replace both <param_name> and {param_name}
        for k, v in params.items():
            str_val = str(v)
            text = text.replace(f"<{k}>", str_val)
            text = text.replace(f"{{{k}}}", str_val)
        return text

    def _execute_script(self, script_content: str, interpreter: Optional[str], logs: List[str]) -> Tuple[int, str]:
        """Runs the script using the correct interpreter and returns (exit_code, stdout+stderr)."""
        suffix = ".sh"
        if sys.platform == "win32":
            suffix = ".ps1" if interpreter == "powershell" else ".bat"
        if interpreter in ["python", "python3"]:
            suffix = ".py"

        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as temp_file:
            temp_file.write(script_content)
            temp_path = temp_file.name

        try:
            # Build command list
            if interpreter == "powershell":
                cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", temp_path]
            elif interpreter in ["python", "python3"]:
                cmd = [sys.executable, temp_path]
            elif interpreter == "bash":
                cmd = ["bash", temp_path]
            else:
                # Direct OS execution (shell = True)
                cmd = temp_path

            shell_mode = (sys.platform == "win32" and interpreter is None)
            res = subprocess.run(
                cmd, capture_output=True, text=True, shell=shell_mode, timeout=60
            )
            output = f"Stdout:\n{res.stdout}\nStderr:\n{res.stderr}"
            return res.returncode, output
        except subprocess.TimeoutExpired:
            return -1, "Execution timed out (60 seconds)"
        except Exception as e:
            return -1, f"Execution failed: {e}"
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    def run_task(self, name: str, user_params: Dict[str, Any]) -> Tuple[bool, List[str]]:
        task = self.load_task(name)
        if not task:
            return False, [f"Task '{name}' not found."]

        logs = [f"Starting execution of task '{name}' (v{task.version})"]
        
        # 1. Resolve and validate parameters
        resolved_params = {}
        task_params = task.parameters or {}
        for param_name, param_spec in task_params.items():
            val = user_params.get(param_name, param_spec.default)
            if param_spec.required and val is None:
                err = f"Missing required parameter '{param_name}'"
                logs.append(err)
                return False, logs
            resolved_params[param_name] = val

        # 2. Execute actions
        rollback_needed = False
        success = True

        for idx, action in enumerate(task.actions):
            logs.append(f"Running Action {idx + 1}/{len(task.actions)} ({action.type})")
            
            if action.type in ["command", "script"]:
                command_text = action.command
                if not command_text:
                    logs.append("Action is missing 'command' string.")
                    success = False
                    rollback_needed = True
                    break
                
                # Parameter substitution
                exec_cmd = self._substitute_params(command_text, resolved_params)
                
                exit_code, output = self._execute_script(exec_cmd, action.interpreter, logs)
                logs.append(f"Result (Exit code: {exit_code}):\n{output}")
                
                if exit_code != 0:
                    success = False
                    rollback_needed = True
                    logs.append(f"Action {idx + 1} failed.")
                    break
            else:
                logs.append(f"Unsupported action type '{action.type}'")
                success = False
                rollback_needed = True
                break

        # 3. Validation
        if success and task.validation:
            logs.append("Running Validation Script...")
            val_script = self._substitute_params(task.validation.script or "", resolved_params)
            exit_code, output = self._execute_script(val_script, task.validation.interpreter, logs)
            logs.append(f"Validation Result (Exit code: {exit_code}):\n{output}")
            if exit_code != 0:
                success = False
                rollback_needed = True
                logs.append("Validation failed.")

        # 4. Rollback
        if rollback_needed and task.rollback:
            logs.append("Triggering Rollback...")
            rb_script = self._substitute_params(task.rollback.script or "", resolved_params)
            exit_code, output = self._execute_script(rb_script, task.rollback.interpreter, logs)
            logs.append(f"Rollback Result (Exit code: {exit_code}):\n{output}")
            if exit_code != 0:
                logs.append("WARNING: Rollback execution failed.")

        logs.append(f"Task finished. Status: {'SUCCESS' if success else 'FAILED'}")
        return success, logs
