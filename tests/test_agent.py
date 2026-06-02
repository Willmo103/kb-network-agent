import json
import pytest
from pathlib import Path
from kb_network_agent.models import TelemetryData, GPUInfo
from kb_network_agent.agent import TelemetryCacheManager

def test_telemetry_model_validation():
    # Test valid model creation
    data = {
        "hostname": "test-host",
        "ip_address": "192.168.1.10",
        "mac_address": "00:11:22:33:44:55",
        "user": "test-user",
        "os_name": "Linux",
        "os_version": "Ubuntu 22.04",
        "cpu_percent": 15.5,
        "cpu_cores": 4,
        "ram_total": 8589934592,
        "ram_used": 4294967296,
        "ram_free": 4294967296,
        "disks": [],
        "timestamp": "2026-06-02T12:00:00"
    }
    telemetry = TelemetryData(**data)
    assert telemetry.hostname == "test-host"
    assert telemetry.gpu is None

def test_cache_manager(tmp_path):
    db_file = tmp_path / "test_cache.db"
    cache_mgr = TelemetryCacheManager(db_path=db_file)
    
    data = TelemetryData(
        hostname="test-host",
        ip_address="192.168.1.10",
        mac_address="00:11:22:33:44:55",
        user="test-user",
        os_name="Linux",
        os_version="Ubuntu",
        cpu_percent=10.0,
        cpu_cores=2,
        ram_total=4000,
        ram_used=2000,
        ram_free=2000,
        disks=[],
        timestamp="2026-06-02T12:00:00"
    )
    
    # Cache it
    cache_mgr.cache_telemetry(data)
    
    # Read it back
    cached = cache_mgr.pop_all_cached()
    assert len(cached) == 1
    assert cached[0].hostname == "test-host"
    
    # Cache should be cleared after pop
    cached_empty = cache_mgr.pop_all_cached()
    assert len(cached_empty) == 0
