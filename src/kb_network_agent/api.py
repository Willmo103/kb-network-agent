import os
import json
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from kb_network_agent.models import TelemetryData
from kb_network_agent.agent import collect_telemetry, CONFIG_FILE
from kb_network_agent.tasks import TaskRunner

app = FastAPI(title="kb-network-agent API", version="0.1.0")
security = HTTPBearer()

# Load token from config
def get_token() -> str:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                return config.get("api_token", "")
        except Exception:
            pass
    return ""

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    expected = get_token()
    # If no token is configured, we reject all authenticated endpoints
    if not expected or credentials.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Token"
        )
    return credentials.credentials

# Initialize task runner
runner = TaskRunner()

@app.get("/status")
def read_status():
    return {"status": "running", "agent": "kb-network-agent"}

@app.get("/telemetry", response_model=TelemetryData)
def get_telemetry_data(token: str = Depends(verify_token)):
    try:
        return collect_telemetry()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to collect telemetry: {e}")

@app.get("/tasks")
def list_tasks(token: str = Depends(verify_token)):
    return {"tasks": runner.list_tasks()}

@app.get("/tasks/export/{name}")
def export_task(name: str, token: str = Depends(verify_token)):
    task = runner.load_task(name)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task.model_dump()}

@app.post("/tasks/import")
def import_task(task_payload: Dict[str, Any], token: str = Depends(verify_token)):
    json_str = json.dumps(task_payload)
    success, errors = runner.import_task(json_str)
    if not success:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"status": "success", "message": f"Task successfully imported."}

@app.delete("/tasks/remove/{name}")
def remove_task(name: str, token: str = Depends(verify_token)):
    if runner.remove_task(name):
        return {"status": "success", "message": f"Task '{name}' removed."}
    raise HTTPException(status_code=404, detail="Task not found")

@app.post("/tasks/run/{name}")
def run_task(name: str, params: Dict[str, Any] = {}, token: str = Depends(verify_token)):
    success, logs = runner.run_task(name, params)
    if not success:
        raise HTTPException(status_code=500, detail={"logs": logs})
    return {"status": "success", "logs": logs}
