"""
Real Raspberry Pi Hotspot Networking Service

Handles:
- hostapd configuration generation
- dnsmasq DHCP/DNS setup
- static subnet management
- automatic hotspot recovery
- classroom network diagnostics
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from shared.file_utils import atomic_write_json, atomic_write_text


class Hostapd:
    """hostapd configuration and management"""

    def __init__(self, interface: str = "wlan0", config_dir: Path | None = None) -> None:
        self.interface = interface
        self.config_dir = config_dir or Path("/etc/hostapd")
        self.config_file = self.config_dir / "hostapd.conf"
        self.enabled = False

    def generate_config(
        self,
        ssid: str,
        passphrase: str,
        country: str = "US",
        channel: int = 6,
        hw_mode: str = "g",
    ) -> str:
        """Generate hostapd configuration"""
        clean_ssid = ssid.replace("\r", "").replace("\n", "")
        clean_passphrase = passphrase.replace("\r", "").replace("\n", "")
        if not clean_ssid or not clean_passphrase:
            raise ValueError("SSID and passphrase must be non-empty and cannot contain newlines")

        config = f"""
interface={self.interface}
driver=nl80211
ssid={clean_ssid}
hw_mode={hw_mode}
channel={channel}
wmm_enabled=1
wpa=2
wpa_passphrase={clean_passphrase}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
wpa_ptk_rekey=600
country_code={country}
ieee80211d=1
"""
        return config.strip()

    def apply_config(
        self,
        ssid: str,
        passphrase: str,
        country: str = "US",
        channel: int = 6,
    ) -> dict[str, Any]:
        """Apply hostapd configuration"""
        config = self.generate_config(ssid, passphrase, country, channel)

        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.config_file, config)
            return {
                "status": "ok",
                "interface": self.interface,
                "ssid": ssid,
                "config_file": str(self.config_file),
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    def is_running(self) -> bool:
        """Check if hostapd is running"""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "hostapd"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


class DNSMasq:
    """dnsmasq DHCP/DNS configuration and management"""

    def __init__(self, interface: str = "wlan0", config_dir: Path | None = None) -> None:
        self.interface = interface
        self.config_dir = config_dir or Path("/etc/dnsmasq.d")
        self.config_file = self.config_dir / "pihub-dhcp.conf"
        self.enabled = False

    def generate_config(
        self,
        subnet: str = "192.168.42.0/24",
        dhcp_range_start: str = "192.168.42.50",
        dhcp_range_end: str = "192.168.42.100",
        lease_time: str = "12h",
    ) -> str:
        """Generate dnsmasq configuration"""
        config = f"""
interface={self.interface}
dhcp-range={dhcp_range_start},{dhcp_range_end},{lease_time}
address=/#/192.168.42.1
no-resolv
server=8.8.8.8
server=8.8.4.4
log-facility=/var/log/dnsmasq.log
"""
        return config.strip()

    def apply_config(
        self,
        subnet: str = "192.168.42.0/24",
        dhcp_range_start: str = "192.168.42.50",
        dhcp_range_end: str = "192.168.42.100",
    ) -> dict[str, Any]:
        """Apply dnsmasq configuration"""
        config = self.generate_config(subnet, dhcp_range_start, dhcp_range_end)

        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.config_file, config)
            return {
                "status": "ok",
                "interface": self.interface,
                "subnet": subnet,
                "config_file": str(self.config_file),
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    def is_running(self) -> bool:
        """Check if dnsmasq is running"""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "dnsmasq"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


class HotspotNetworking:
    """Real Raspberry Pi hotspot networking coordination"""

    def __init__(
        self,
        interface: str = "wlan0",
        config_dir: Path | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.interface = interface
        self.config_dir = config_dir or Path("/etc/pihub/network")
        self.state_file = state_file or self.config_dir / "hotspot_state.json"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.hostapd = Hostapd(interface, Path("/etc/hostapd"))
        self.dnsmasq = DNSMasq(interface, Path("/etc/dnsmasq.d"))
        self.hotspot_enabled = False
        self.last_check = 0
        self._load_state()

    def _load_state(self) -> None:
        """Load hotspot state from disk"""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self.hotspot_enabled = data.get("hotspot_enabled", False)
            except Exception:
                pass

    def _save_state(self) -> None:
        """Persist hotspot state (crash-safe: tmp + fsync + rename)"""
        data = {
            "hotspot_enabled": self.hotspot_enabled,
            "last_updated": int(time.time()),
            "interface": self.interface,
        }
        atomic_write_json(self.state_file, data)

    def setup_hotspot(
        self,
        ssid: str,
        passphrase: str,
        static_ip: str = "192.168.42.1",
        netmask: str = "255.255.255.0",
    ) -> dict[str, Any]:
        """Setup classroom hotspot"""
        hostapd_result = self.hostapd.apply_config(ssid, passphrase)
        if hostapd_result["status"] != "ok":
            return hostapd_result

        dnsmasq_result = self.dnsmasq.apply_config()
        if dnsmasq_result["status"] != "ok":
            return dnsmasq_result

        self.hotspot_enabled = True
        self._save_state()

        return {
            "status": "ok",
            "ssid": ssid,
            "interface": self.interface,
            "static_ip": static_ip,
            "netmask": netmask,
            "hostapd": hostapd_result,
            "dnsmasq": dnsmasq_result,
        }

    def check_hotspot_health(self) -> dict[str, Any]:
        """Check hotspot operational health"""
        now = int(time.time())
        if now - self.last_check < 5:
            return {"status": "cached"}

        hostapd_running = self.hostapd.is_running()
        dnsmasq_running = self.dnsmasq.is_running()

        self.last_check = now

        return {
            "hotspot_enabled": self.hotspot_enabled,
            "hostapd_running": hostapd_running,
            "dnsmasq_running": dnsmasq_running,
            "interface": self.interface,
            "status": "healthy" if hostapd_running and dnsmasq_running else "degraded",
        }

    def recover_hotspot(self) -> dict[str, Any]:
        """Attempt to recover failed hotspot"""
        health = self.check_hotspot_health()

        if not health["hostapd_running"]:
            try:
                subprocess.run(["systemctl", "start", "hostapd"], timeout=10)
            except Exception:
                pass

        if not health["dnsmasq_running"]:
            try:
                subprocess.run(["systemctl", "start", "dnsmasq"], timeout=10)
            except Exception:
                pass

        return self.check_hotspot_health()

    def get_connected_devices(self) -> dict[str, Any]:
        """Get devices connected to hotspot"""
        try:
            result = subprocess.run(
                ["iw", "dev", self.interface, "station", "dump"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            stations = result.stdout.count("Station")
            return {"interface": self.interface, "connected_devices": stations}
        except Exception:
            return {"interface": self.interface, "connected_devices": 0, "error": "unable_to_query"}
