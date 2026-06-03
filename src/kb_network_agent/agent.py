import os
import sys
import socket
import shutil
import subprocess
import psutil
import logging
import json
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
import httpx

from kb_network_agent.models import (
    TelemetryData,
    GPUInfo,
    DiskInfo,
    DockerContainer,
    DockerStats,
    OllamaModel,
    OllamaStats,
    DatabaseStatus,
    DatabaseStats,
    PendingUpdate,
    SystemUpdates,
)

logger = logging.getLogger("kb-network-agent")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)

# Default directories
KB_ROOT = Path.home() / ".kb"
CACHE_DB = KB_ROOT / "agent_cache.db"
CONFIG_FILE = KB_ROOT / "configs" / "kb-network-agent.json"


def ensure_kb_dirs():
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    (KB_ROOT / "configs").mkdir(parents=True, exist_ok=True)
    (KB_ROOT / "tasks").mkdir(parents=True, exist_ok=True)


class TelemetryCacheManager:
    def __init__(self, db_path: Path = CACHE_DB):
        self.db_path = db_path
        ensure_kb_dirs()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT,
                    timestamp TEXT
                )
            """
            )
            conn.commit()

    def cache_telemetry(self, data: TelemetryData):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO telemetry_cache (data, timestamp) VALUES (?, ?)",
                    (data.model_dump_json(), data.timestamp),
                )
                conn.commit()
            logger.info("Cached telemetry locally.")
        except Exception as e:
            logger.error(f"Failed to cache telemetry locally: {e}")

    def pop_all_cached(self) -> List[TelemetryData]:
        cached = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, data FROM telemetry_cache ORDER BY id ASC")
                rows = cursor.fetchall()
                for row_id, data_str in rows:
                    try:
                        cached.append(
                            (row_id, TelemetryData.model_validate_json(data_str))
                        )
                    except Exception as parse_err:
                        logger.error(
                            f"Failed to parse cached telemetry ID {row_id}: {parse_err}"
                        )
                        # delete bad data
                        conn.execute(
                            "DELETE FROM telemetry_cache WHERE id = ?", (row_id,)
                        )

                # Delete rows that were successfully loaded
                if cached:
                    ids = [c[0] for c in cached]
                    conn.execute(
                        f"DELETE FROM telemetry_cache WHERE id IN ({','.join('?' for _ in ids)})",
                        ids,
                    )
                    conn.commit()
        except Exception as e:
            logger.error(f"Failed to fetch cached telemetry: {e}")
        return [c[1] for c in cached]


# ----------------------------------------------------
# Telemetry Collection Functions
# ----------------------------------------------------


def get_ip_address() -> str:
    try:
        # Connect to an external host to determine local IP used on the primary interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        # Fallback to localhost IPs
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def get_mac_address() -> str:
    try:
        for interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == psutil.AF_LINK or (
                    hasattr(psutil, "AF_PACKET") and addr.family == psutil.AF_PACKET
                ):
                    if addr.address and addr.address != "00:00:00:00:00:00":
                        return addr.address
    except Exception:
        pass
    return "00:00:00:00:00:00"


def get_dns_servers() -> List[str]:
    dns_servers = []
    if sys.platform == "win32":
        try:
            cmd = "Get-DnsClientServerAddress | Where-Object {$_.ServerAddresses -ne $null} | Select-Object -ExpandProperty ServerAddresses"
            res = subprocess.run(
                ["powershell", "-Command", cmd],
                capture_output=True,
                text=True,
                shell=True,
            )
            dns_servers = list(
                set(
                    [
                        line.strip()
                        for line in res.stdout.strip().split("\n")
                        if line.strip()
                    ]
                )
            )
        except Exception as e:
            logger.debug(f"Failed to get DNS servers via PowerShell: {e}")
    else:
        try:
            if os.path.exists("/etc/resolv.conf"):
                with open("/etc/resolv.conf", "r") as f:
                    for line in f:
                        if line.startswith("nameserver"):
                            dns_servers.append(line.split()[1])
        except Exception as e:
            logger.debug(f"Failed to read /etc/resolv.conf: {e}")
    return dns_servers


def collect_gpu_info() -> Optional[GPUInfo]:
    # 1. Nvidia
    if shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=gpu_name,driver_version,memory.total,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            parts = res.stdout.strip().split(",")
            if len(parts) >= 4:
                return GPUInfo(
                    name=parts[0].strip(),
                    driver_version=parts[1].strip(),
                    memory_total=int(parts[2].strip()),
                    memory_used=int(parts[3].strip()),
                    utilization=None,
                )
        except Exception as e:
            logger.debug(f"nvidia-smi check failed: {e}")

    # 2. AMD
    if shutil.which("rocm-smi"):
        try:
            # Basic AMD check
            res = subprocess.run(
                ["rocm-smi", "--showproductname"], capture_output=True, text=True
            )
            if "Card" in res.stdout:
                return GPUInfo(name="AMD Radeon GPU (rocm)", utilization=None)
        except Exception:
            pass

    # 3. Fallback check for Display controller hardware details
    if sys.platform == "win32":
        try:
            cmd = "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"
            res = subprocess.run(
                ["powershell", "-Command", cmd],
                capture_output=True,
                text=True,
                shell=True,
            )
            gpu_name = res.stdout.strip()
            if gpu_name:
                return GPUInfo(name=gpu_name.split("\n")[0])
        except Exception:
            pass
    else:
        if shutil.which("lshw"):
            try:
                res = subprocess.run(
                    ["lshw", "-c", "display"], capture_output=True, text=True
                )
                for line in res.stdout.split("\n"):
                    if "product:" in line:
                        gpu_name = line.split("product:")[-1].strip()
                        return GPUInfo(name=gpu_name)
            except Exception:
                pass

    return None


def collect_disk_info() -> List[DiskInfo]:
    disks = []
    try:
        for part in psutil.disk_partitions(all=False):
            if os.name == "nt" and "cdrom" in part.opts:
                continue
            if not part.mountpoint:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append(
                    DiskInfo(
                        path=part.mountpoint,
                        total=usage.total,
                        used=usage.used,
                        free=usage.free,
                        percent=usage.percent,
                    )
                )
            except PermissionError:
                # Disk not ready, e.g., floppy, CD drive
                continue
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Error collecting disk info: {e}")
    return disks


def collect_docker_info() -> Optional[DockerStats]:
    try:
        import docker

        client = docker.from_env()
        version = client.version().get("Version", "Unknown")
        containers = client.containers.list(all=True)
        active_count = sum(1 for c in containers if c.status == "running")

        container_list = [
            DockerContainer(
                id=c.short_id,
                name=c.name,
                image=c.image.tags[0] if c.image.tags else c.image.id[:12],
                status=c.status,
                state=c.attrs.get("State", {}).get("Status", "unknown"),
            )
            for c in containers
        ]
        return DockerStats(
            version=version,
            active_count=active_count,
            total_count=len(containers),
            containers=container_list,
        )
    except Exception as e:
        logger.debug(f"Docker API unavailable: {e}")
        return None


def collect_ollama_info() -> Optional[OllamaStats]:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        if r.status_code == 200:
            models_data = r.json().get("models", [])
            models = []
            for m in models_data:
                details = m.get("details", {})
                models.append(
                    OllamaModel(
                        name=m.get("name"),
                        size=m.get("size"),
                        digest=m.get("digest"),
                        format=details.get("format"),
                        family=details.get("family"),
                    )
                )

            version = "Unknown"
            try:
                rv = httpx.get("http://localhost:11434/api/version", timeout=1.0)
                if rv.status_code == 200:
                    version = rv.json().get("version", "Unknown")
            except Exception:
                pass

            return OllamaStats(version=version, api_version="v1", models=models)
    except Exception:
        pass
    return None


def collect_database_info() -> DatabaseStats:
    db_ports = {
        "postgresql": 5432,
        "redis": 6379,
        "rabbitmq": 5672,
        "minio": 9000,
        "nginx": 80,
    }
    databases = []
    for db_type, port in db_ports.items():
        # Check port status
        running = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                running = True
        except OSError:
            pass

        databases.append(DatabaseStatus(type=db_type, running=running, version=None))
    return DatabaseStats(databases=databases)


def collect_updates_info() -> SystemUpdates:
    updates = []
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["winget", "upgrade"], capture_output=True, text=True, shell=True
            )
            start_parsing = False
            for line in res.stdout.strip().split("\n"):
                if line.startswith("---"):
                    start_parsing = True
                    continue
                if start_parsing and line.strip():
                    # Parse winget table row
                    parts = [p.strip() for p in line.split("  ") if p.strip()]
                    if len(parts) >= 4:
                        updates.append(
                            PendingUpdate(
                                name=parts[0],
                                current_version=parts[2],
                                new_version=parts[3],
                                source="winget",
                            )
                        )
        except Exception as e:
            logger.debug(f"winget upgrade check failed: {e}")
    else:
        if shutil.which("apt"):
            try:
                # Update package index and list upgradable
                # Using --upgradable lists upgradable packages
                res = subprocess.run(
                    ["apt", "list", "--upgradable"], capture_output=True, text=True
                )
                for line in res.stdout.strip().split("\n"):
                    if "upgradable from:" in line:
                        # e.g., curl/jammy-updates 7.81.0-1ubuntu1.16 amd64 [upgradable from: 7.81.0-1ubuntu1.15]
                        parts = line.split("/")
                        pkg_name = parts[0].strip()
                        version_part = line.split(" ")
                        new_version = version_part[1] if len(version_part) > 1 else None
                        old_version = None
                        if "upgradable from:" in line:
                            old_version = (
                                line.split("upgradable from:")[-1]
                                .replace("]", "")
                                .strip()
                            )

                        updates.append(
                            PendingUpdate(
                                name=pkg_name,
                                current_version=old_version,
                                new_version=new_version,
                                source="apt",
                            )
                        )
            except Exception as e:
                logger.debug(f"apt check failed: {e}")

    return SystemUpdates(
        pending_count=len(updates),
        updates=updates,
        last_check=datetime.now().isoformat(),
    )


def collect_telemetry() -> TelemetryData:
    hostname = socket.gethostname()
    ip_address = get_ip_address()
    mac_address = get_mac_address()

    try:
        user = os.getlogin()
    except Exception:
        user = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))

    os_name = sys.platform
    os_version = sys.version

    if sys.platform == "win32":
        os_name = "Windows"
        os_version = (
            f"Windows {platform_version()}"
            if shutil.which("powershell")
            else sys.getwindowsversion().service_pack or "Windows"
        )
    elif sys.platform == "linux":
        os_name = "Linux"
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        os_version = line.split("=")[1].strip().replace('"', "")
        except Exception:
            pass

    cpu_percent = psutil.cpu_percent(interval=0.1)
    cpu_cores = psutil.cpu_count(logical=True) or 1

    svmem = psutil.virtual_memory()
    ram_total = svmem.total
    ram_used = svmem.used
    ram_free = svmem.available

    disks = collect_disk_info()
    dns_servers = get_dns_servers()

    # Listening ports
    listening_ports = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN":
                listening_ports.append(conn.laddr.port)
        listening_ports = sorted(list(set(listening_ports)))
    except Exception:
        pass

    # Fat Struct placeholders
    gpu = collect_gpu_info()
    docker_stats = collect_docker_info()
    ollama_stats = collect_ollama_info()
    db_stats = collect_database_info()
    updates = collect_updates_info()

    return TelemetryData(
        hostname=hostname,
        ip_address=ip_address,
        mac_address=mac_address,
        user=user,
        os_name=os_name,
        os_version=os_version,
        cpu_percent=cpu_percent,
        cpu_cores=cpu_cores,
        ram_total=ram_total,
        ram_used=ram_used,
        ram_free=ram_free,
        disks=disks,
        listening_ports=listening_ports,
        open_ports=[],  # populated by server or user configuration
        dns_servers=dns_servers,
        gpu=gpu,
        docker=docker_stats,
        ollama=ollama_stats,
        databases=db_stats,
        updates=updates,
        timestamp=datetime.now().isoformat(),
    )


def platform_version() -> str:
    try:
        res = subprocess.run(
            ["powershell", "[System.Environment]::OSVersion.Version.ToString()"],
            capture_output=True,
            text=True,
            shell=True,
        )
        return res.stdout.strip()
    except Exception:
        return "10.0"


# ----------------------------------------------------
# Main daemon loop triggers
# ----------------------------------------------------
def send_telemetry_to_server(server_url: str, token: str, data: TelemetryData) -> bool:
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = httpx.post(
            f"{server_url}/telemetry",
            json=data.model_dump(),
            headers=headers,
            timeout=5.0,
        )
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Failed to post telemetry to {server_url}: {e}")
        return False
