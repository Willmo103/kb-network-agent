from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class GPUInfo(BaseModel):
    name: str
    driver_version: Optional[str] = None
    memory_total: Optional[int] = None  # in MB
    memory_used: Optional[int] = None  # in MB
    utilization: Optional[float] = None  # percentage


class DiskInfo(BaseModel):
    path: str
    total: int  # bytes
    used: int  # bytes
    free: int  # bytes
    percent: float


class DockerContainer(BaseModel):
    id: str
    name: str
    image: str
    status: str
    state: str


class DockerStats(BaseModel):
    version: Optional[str] = None
    active_count: int
    total_count: int
    containers: List[DockerContainer] = []


class OllamaModel(BaseModel):
    name: str
    size: Optional[int] = None
    digest: Optional[str] = None
    format: Optional[str] = None
    family: Optional[str] = None


class OllamaStats(BaseModel):
    version: Optional[str] = None
    api_version: Optional[str] = None
    models: List[OllamaModel] = []


class DatabaseStatus(BaseModel):
    type: str  # e.g. postgres, redis, sqlite, rabbitmq, minio, nginx
    running: bool
    version: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class DatabaseStats(BaseModel):
    databases: List[DatabaseStatus] = []


class PendingUpdate(BaseModel):
    name: str
    current_version: Optional[str] = None
    new_version: Optional[str] = None
    source: str  # e.g. apt, winget


class SystemUpdates(BaseModel):
    pending_count: int
    updates: List[PendingUpdate] = []
    last_check: str


class TelemetryData(BaseModel):
    hostname: str
    ip_address: str
    mac_address: str
    user: str
    os_name: str
    os_version: str
    cpu_percent: float
    cpu_cores: int
    ram_total: int  # bytes
    ram_used: int  # bytes
    ram_free: int  # bytes
    disks: List[DiskInfo] = []
    listening_ports: List[int] = []
    open_ports: List[int] = []
    dns_servers: List[str] = []
    gpu: Optional[GPUInfo] = None
    docker: Optional[DockerStats] = None
    ollama: Optional[OllamaStats] = None
    databases: Optional[DatabaseStats] = None
    updates: Optional[SystemUpdates] = None
    timestamp: str
