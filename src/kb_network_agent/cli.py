import os
import sys
import json
import time
import shutil
import signal
import threading
import subprocess
import logging
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table
import psutil

from kb_network_agent.agent import (
    collect_telemetry,
    send_telemetry_to_server,
    TelemetryCacheManager,
    KB_ROOT,
    CONFIG_FILE,
    ensure_kb_dirs,
)
from kb_network_agent.tasks import TaskRunner, validate_task_json

app = typer.Typer(
    help="kb-network-agent CLI for telemetry monitoring and task execution."
)
tasks_app = typer.Typer(help="Manage and run custom tasks.")
app.add_typer(tasks_app, name="tasks")

console = Console()
PID_FILE = KB_ROOT / "kb-network-agent.pid"
LOG_FILE = KB_ROOT / "kb-network-agent.log"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "server_url": "http://localhost:8082",
        "api_token": "dev_token_123",
        "port": 8081,
        "interval_seconds": 60,
    }


def save_config(config: dict):
    ensure_kb_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def is_pid_running(pid: int) -> bool:
    return psutil.pid_exists(pid)


# ----------------------------------------------------
# Main Agent daemon loop
# ----------------------------------------------------
def telemetry_loop():
    cache_mgr = TelemetryCacheManager()
    while True:
        try:
            config = load_config()
            data = collect_telemetry()
            server_url = config.get("server_url")
            token = config.get("api_token")

            if server_url and token:
                # Attempt sending
                success = send_telemetry_to_server(server_url, token, data)
                if success:
                    # Flush cached telemetry
                    cached = cache_mgr.pop_all_cached()
                    for c_data in cached:
                        send_telemetry_to_server(server_url, token, c_data)
                else:
                    cache_mgr.cache_telemetry(data)
            else:
                cache_mgr.cache_telemetry(data)
        except Exception as e:
            logging.error(f"Error in telemetry loop: {e}")

        config = load_config()
        time.sleep(config.get("interval_seconds", 60))


@app.command(hidden=True)
def run_daemon():
    """Runs the uvicorn API server and the telemetry loop in the foreground."""
    ensure_kb_dirs()

    # Configure file logging
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(log_formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)

    # Start telemetry collector thread
    t = threading.Thread(target=telemetry_loop, daemon=True)
    t.start()

    # Start FastAPI server
    config = load_config()
    import uvicorn

    logger = logging.getLogger("uvicorn")
    logger.addHandler(file_handler)

    uvicorn.run(
        "kb_network_agent.api:app",
        host="0.0.0.0",
        port=config.get("port", 8081),
        log_level="info",
    )


# ----------------------------------------------------
# Service Management Commands
# ----------------------------------------------------
@app.command()
def start():
    """Starts the agent daemon process in the background."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if is_pid_running(pid):
                console.print(
                    f"[yellow]Daemon is already running (PID: {pid}).[/yellow]"
                )
                raise typer.Exit()
        except ValueError:
            pass

    console.print("Starting kb-network-agent daemon...")
    python_exe = sys.executable

    if sys.platform == "win32":
        # Resolve pythonw from virtual environment prefix first
        venv_pythonw = Path(sys.prefix) / "Scripts" / "pythonw.exe"
        venv_python = Path(sys.prefix) / "Scripts" / "python.exe"
        if venv_pythonw.exists():
            pythonw = str(venv_pythonw)
        elif venv_python.exists():
            pythonw = str(venv_python)
        else:
            pythonw = sys.executable.replace("python.exe", "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable

        # Detach cmd window completely using creationflags
        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            [pythonw, "-m", "kb_network_agent.cli", "run-daemon"],
            creationflags=DETACHED_PROCESS,
            close_fds=True,
        )
        pid = proc.pid
    else:
        # Linux / MacOS daemonization
        proc = subprocess.Popen(
            [python_exe, "-m", "kb_network_agent.cli", "run-daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp,
            close_fds=True,
        )
        pid = proc.pid

    PID_FILE.write_text(str(pid))
    console.print(f"[green]Daemon started in background (PID: {pid}).[/green]")


@app.command()
def stop():
    """Stops the background agent daemon process."""
    if not PID_FILE.exists():
        console.print("[yellow]No daemon PID file found. Is it running?[/yellow]")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        if is_pid_running(pid):
            console.print(f"Stopping daemon (PID: {pid})...")
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)], capture_output=True
                )
            else:
                os.kill(pid, signal.SIGTERM)

            # Wait for shutdown
            for _ in range(5):
                if not is_pid_running(pid):
                    break
                time.sleep(0.5)
            console.print("[green]Daemon stopped.[/green]")
        else:
            console.print("[yellow]Daemon process not active.[/yellow]")
    except Exception as e:
        console.print(f"[red]Error stopping daemon: {e}[/red]")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


@app.command()
def restart():
    """Restarts the agent daemon process."""
    stop()
    time.sleep(1)
    start()


@app.command()
def status():
    """Checks the status of the daemon."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if is_pid_running(pid):
                console.print(
                    f"[green]kb-network-agent daemon is running (PID: {pid}).[/green]"
                )
                return
        except ValueError:
            pass
    console.print("[red]kb-network-agent daemon is stopped.[/red]")


@app.command()
def logs(lines: int = typer.Option(50, help="Number of lines to show.")):
    """Shows the background daemon log output."""
    if not LOG_FILE.exists():
        console.print("[yellow]No log file found.[/yellow]")
        return
    with open(LOG_FILE, "r") as f:
        log_lines = f.readlines()
        for line in log_lines[-lines:]:
            print(line, end="")


@app.command()
def install(
    server_url: str = typer.Option(
        "http://localhost:8082", prompt=True, help="Central monitoring server URL"
    ),
    api_token: str = typer.Option(
        "dev_token_123", prompt=True, help="Secure API token to authorize agent"
    ),
    port: int = typer.Option(
        8081, prompt=True, help="Local agent port to run API server on"
    ),
    interval: int = typer.Option(60, prompt=True, help="Polling interval in seconds"),
):
    """Installs the agent configurations and registers it to startup."""
    config = {
        "server_url": server_url,
        "api_token": api_token,
        "port": port,
        "interval_seconds": interval,
    }
    save_config(config)
    console.print(f"[green]Configurations saved to {CONFIG_FILE}[/green]")

    # Setup OS startup triggers
    if sys.platform == "win32":
        startup_dir = (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        bat_file = startup_dir / "kb-network-agent-launcher.bat"
        python_exe = sys.executable
        # Create silent launcher bat file using pythonw
        pythonw = python_exe.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = python_exe

        bat_content = f'@echo off\nstart "" "{pythonw}" -m kb_network_agent.cli start\n'
        try:
            bat_file.write_text(bat_content)
            console.print(
                f"[green]Registered startup batch script in {bat_file}[/green]"
            )
        except Exception as e:
            console.print(f"[red]Failed to register startup batch script: {e}[/red]")
    else:
        console.print(
            "[yellow]On Linux/macOS, please configure kb-network-agent as a systemd service or launchd agent.[/yellow]"
        )


@app.command()
def update():
    """Performs self-upgrade using uv tool update."""
    console.print("Running agent self-update...")
    try:
        subprocess.run(["uv", "tool", "update", "kb-network-agent"], check=True)
        console.print("[green]Agent successfully updated.[/green]")
    except Exception as e:
        console.print(f"[red]Failed to update agent: {e}[/red]")


# ----------------------------------------------------
# Task Runner subcommands
# ----------------------------------------------------
@tasks_app.command(name="ls")
def tasks_list():
    """Lists all configured tasks on this agent."""
    runner = TaskRunner()
    tasks = runner.list_tasks()
    if not tasks:
        console.print("No tasks configured.")
        return

    table = Table(title="Imported Tasks")
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="magenta")
    table.add_column("Description")
    table.add_column("Target Type", style="green")

    for t in tasks:
        table.add_row(t["name"], t["version"], t["description"], t["target"]["type"])
    console.print(table)


@tasks_app.command(name="validate")
def tasks_validate(path: Path):
    """Validates a task definition file programmatically."""
    if not path.exists():
        console.print(f"[red]File {path} does not exist.[/red]")
        raise typer.Exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        valid, errors = validate_task_json(content)
        if valid:
            console.print("[green]Task schema is valid![/green]")
        else:
            console.print("[red]Task schema validation failed:[/red]")
            for err in errors:
                console.print(f" - {err}")
            raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error reading file: {e}[/red]")
        raise typer.Exit(1)


@tasks_app.command(name="add")
def tasks_add(path: Path):
    """Imports and validates a new task definition into the agent."""
    if not path.exists():
        console.print(f"[red]File {path} does not exist.[/red]")
        raise typer.Exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        runner = TaskRunner()
        success, messages = runner.import_task(content)
        if success:
            for msg in messages:
                console.print(f"[green]{msg}[/green]")
        else:
            console.print("[red]Import failed with validation errors:[/red]")
            for err in messages:
                console.print(f" - {err}")
            raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error importing task: {e}[/red]")
        raise typer.Exit(1)


@tasks_app.command(name="remove")
def tasks_remove(name: str):
    """Removes a task from the agent."""
    runner = TaskRunner()
    if runner.remove_task(name):
        console.print(f"[green]Task '{name}' successfully removed.[/green]")
    else:
        console.print(f"[red]Task '{name}' not found.[/red]")


@tasks_app.command(name="run")
def tasks_run(
    name: str,
    params: Optional[str] = typer.Option(
        None, help="JSON string representing parameter key/value overrides"
    ),
):
    """Executes a task locally on the agent."""
    runner = TaskRunner()
    param_dict = {}
    if params:
        try:
            param_dict = json.loads(params)
        except Exception as e:
            console.print(f"[red]Invalid params JSON: {e}[/red]")
            raise typer.Exit(1)

    console.print(f"Executing task '{name}'...")
    success, logs = runner.run_task(name, param_dict)
    for log in logs:
        style = (
            "green"
            if "SUCCESS" in log
            else (
                "red" if "failed" in log.lower() or "rollback" in log.lower() else None
            )
        )
        console.print(log, style=style)

    if not success:
        raise typer.Exit(1)


@tasks_app.command(name="view")
def tasks_view(name: str):
    """Views the raw JSON definition of a task."""
    runner = TaskRunner()
    task = runner.load_task(name)
    if not task:
        console.print(f"[red]Task '{name}' not found.[/red]")
        raise typer.Exit(1)
    console.print_json(data=task.model_dump())


if __name__ == "__main__":
    app()
