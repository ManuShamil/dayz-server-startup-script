import argparse
import os
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

import requests
import yaml


WINDOWS_STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
LINUX_STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
DAYZ_SERVER_APP_ID = 223350
STEAM_GUARD_PROMPTS = (
    "steam guard",
    "steam guard code",
    "two-factor code",
    "enter the current code",
)
LOGIN_FAILURE_MARKERS = (
    "login failure",
    "failed to login",
    "invalid password",
    "account logon denied",
    "two-factor code mismatch",
)

TIMEZONE_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "UTC": "UTC",
}


@dataclass
class RestartWarning:
    offset: timedelta
    message: str


@dataclass
class PeriodicMessage:
    interval: timedelta
    message: str
    first_after_start: timedelta
    next_run_at: Optional[datetime] = None


@dataclass
class RuntimeSchedule:
    next_restart_at: Optional[datetime]
    sent_warning_offsets: set[int]
    periodic_messages: list[PeriodicMessage]


class BERconClient:
    def __init__(self, host: str, port: int, password: str, timeout_seconds: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout_seconds = timeout_seconds
        self._socket: Optional[socket.socket] = None
        self._command_sequence = 0

    def __enter__(self) -> "BERconClient":
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(self.timeout_seconds)
        self.login()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def login(self) -> None:
        response_type, payload = self._request(0x00, self.password.encode("ascii"))
        if response_type != 0x00 or not payload or payload[0] != 0x01:
            raise RuntimeError("BattlEye RCON login failed")

    def send_command(self, command: str) -> str:
        sequence = self._command_sequence
        self._command_sequence = (self._command_sequence + 1) % 256
        response_type, payload = self._request(0x01, bytes([sequence]) + command.encode("utf-8"))
        if response_type != 0x01 or not payload or payload[0] != sequence:
            raise RuntimeError("Unexpected BattlEye RCON command response")
        return self._read_command_response(sequence, payload[1:])

    def _read_command_response(self, sequence: int, initial_payload: bytes) -> str:
        if initial_payload and initial_payload[0] == 0x00 and len(initial_payload) >= 3:
            total_packets = initial_payload[1]
            packet_index = initial_payload[2]
            packets: dict[int, str] = {packet_index: initial_payload[3:].decode("utf-8", errors="replace")}
            while len(packets) < total_packets:
                response_type, payload = self._receive_packet()
                if response_type == 0x02:
                    self._ack_server_message(payload)
                    continue
                if response_type != 0x01 or not payload or payload[0] != sequence:
                    continue
                body = payload[1:]
                if body and body[0] == 0x00 and len(body) >= 3:
                    packets[body[2]] = body[3:].decode("utf-8", errors="replace")
                else:
                    return body.decode("utf-8", errors="replace")
            return "".join(packets[index] for index in sorted(packets))

        return initial_payload.decode("utf-8", errors="replace")

    def _request(self, packet_type: int, payload: bytes) -> tuple[int, bytes]:
        self._send_packet(packet_type, payload)
        while True:
            response_type, response_payload = self._receive_packet()
            if response_type == 0x02:
                self._ack_server_message(response_payload)
                continue
            return response_type, response_payload

    def _ack_server_message(self, payload: bytes) -> None:
        if not payload:
            return
        self._send_packet(0x02, payload[:1])

    def _send_packet(self, packet_type: int, payload: bytes) -> None:
        if self._socket is None:
            raise RuntimeError("BattlEye RCON socket is not connected")

        protocol_body = b"\xff" + bytes([packet_type]) + payload
        checksum = zlib.crc32(protocol_body) & 0xFFFFFFFF
        packet = b"BE" + checksum.to_bytes(4, "little") + protocol_body
        self._socket.sendto(packet, (self.host, self.port))

    def _receive_packet(self) -> tuple[int, bytes]:
        if self._socket is None:
            raise RuntimeError("BattlEye RCON socket is not connected")

        packet, _address = self._socket.recvfrom(65535)
        if len(packet) < 9 or packet[:2] != b"BE" or packet[6] != 0xFF:
            raise RuntimeError("Received malformed BattlEye RCON packet")

        protocol_body = packet[6:]
        expected_checksum = int.from_bytes(packet[2:6], "little")
        actual_checksum = zlib.crc32(protocol_body) & 0xFFFFFFFF
        if expected_checksum != actual_checksum:
            raise RuntimeError("Received BattlEye RCON packet with invalid checksum")

        return packet[7], packet[8:]


class DayZServerManager:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.config = self._load_config()
        self._stop_requested = False
        self._server_process: Optional[subprocess.Popen] = None
        self._scheduler_timezone = self._load_scheduler_timezone()
        self._console_thread: Optional[Thread] = None
        self._console_stop_requested = False
        self._rcon_lock = Lock()

    def _load_config(self) -> dict:
        with self.config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}

        required_sections = ("steamcmd", "steam", "server", "mods")
        missing_sections = [section for section in required_sections if section not in config]
        if missing_sections:
            raise ValueError(f"Missing config section(s): {', '.join(missing_sections)}")

        return config

    def _load_scheduler_timezone(self) -> object:
        scheduler_config = self.config.get("scheduler", {})
        timezone_name = scheduler_config.get("timezone")
        if timezone_name:
            canonical_name = TIMEZONE_ALIASES.get(timezone_name, timezone_name)
            try:
                return ZoneInfo(canonical_name)
            except ZoneInfoNotFoundError:
                print(
                    f"Warning: timezone '{timezone_name}' is not available on this system. "
                    "Falling back to the local system timezone."
                )
        return datetime.now().astimezone().tzinfo

    def resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
        return (self.config_path.parent / path).resolve()

    @property
    def steamcmd_dir(self) -> Path:
        return self.resolve_path(self.config["steamcmd"]["path"])

    @property
    def steamcmd_executable(self) -> Path:
        executable = "steamcmd.exe" if os.name == "nt" else "steamcmd.sh"
        return self.steamcmd_dir / executable

    @property
    def steamcmd_extra_args(self) -> list[str]:
        raw_args = self.config["steamcmd"].get("extra_args", [])
        if isinstance(raw_args, str):
            return shlex.split(raw_args, posix=os.name != "nt")
        return [str(arg) for arg in raw_args]

    @property
    def server_install_dir(self) -> Path:
        return self.resolve_path(self.config["server"]["install_dir"])

    @property
    def server_executable(self) -> Path:
        return self.resolve_path(self.config["server"]["executable"])

    @property
    def mods_directory(self) -> Path:
        return self.resolve_path(self.config["mods"]["directory"])

    @property
    def steamcmd_workshop_directory(self) -> Path:
        workshop_app_id = str(self.config["mods"]["workshop_app_id"])
        return self.steamcmd_dir / "steamapps" / "workshop" / "content" / workshop_app_id

    @property
    def rcon_config(self) -> dict:
        return self.config.get("rcon", {})

    @property
    def scheduler_config(self) -> dict:
        return self.config.get("scheduler", {})

    def ensure_steamcmd(self) -> None:
        if self.steamcmd_executable.exists():
            print(f"SteamCMD found at {self.steamcmd_executable}")
            return

        self.steamcmd_dir.mkdir(parents=True, exist_ok=True)
        archive_name = "steamcmd.zip" if os.name == "nt" else "steamcmd_linux.tar.gz"
        archive_path = self.steamcmd_dir / archive_name
        url = WINDOWS_STEAMCMD_URL if os.name == "nt" else LINUX_STEAMCMD_URL

        print(f"Downloading SteamCMD from {url}")
        self._download_file(url, archive_path)

        print(f"Extracting SteamCMD into {self.steamcmd_dir}")
        if os.name == "nt":
            with zipfile.ZipFile(archive_path, "r") as archive:
                archive.extractall(self.steamcmd_dir)
        else:
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(self.steamcmd_dir)

        try:
            archive_path.unlink()
        except OSError:
            pass

        if os.name != "nt" and self.steamcmd_executable.exists():
            current_mode = self.steamcmd_executable.stat().st_mode
            self.steamcmd_executable.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if not self.steamcmd_executable.exists():
            raise FileNotFoundError(f"SteamCMD executable was not found after extraction: {self.steamcmd_executable}")

    def _download_file(self, url: str, destination: Path) -> None:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        handle.write(chunk)

    def install_or_update_server(self) -> None:
        self.server_install_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "+force_install_dir",
            str(self.server_install_dir),
            "+app_update",
            str(DAYZ_SERVER_APP_ID),
            "validate",
        ]
        print("Installing or updating DayZ dedicated server")
        self.run_steamcmd(command, allow_anonymous_fallback=False)

    def update_mods(self) -> list[Path]:
        self.mods_directory.mkdir(parents=True, exist_ok=True)
        mod_ids = self._extract_mod_ids(self.config["mods"].get("links", []))
        if not mod_ids:
            print("No Workshop mods configured")
            return []

        workshop_app_id = str(self.config["mods"]["workshop_app_id"])
        downloaded_paths: list[Path] = []
        for mod_id in mod_ids:
            print(f"Downloading or updating Workshop mod {mod_id}")
            command = [
                "+workshop_download_item",
                workshop_app_id,
                mod_id,
                "validate",
            ]
            self.run_steamcmd(command, allow_anonymous_fallback=True)
            mod_path = self.steamcmd_workshop_directory / mod_id
            if mod_path.exists():
                downloaded_paths.append(mod_path)
            else:
                print(f"Warning: expected mod path not found after download: {mod_path}")

        return downloaded_paths

    def _extract_mod_ids(self, links: Iterable[str]) -> list[str]:
        mod_ids: list[str] = []
        for link in links:
            parsed = urlparse(link)
            query = parse_qs(parsed.query)
            mod_id = query.get("id", [None])[0]
            if mod_id:
                mod_ids.append(mod_id)
            else:
                print(f"Warning: unable to parse Workshop id from link: {link}")
        return mod_ids

    def _copy_mod_keys(self, mod_paths: Iterable[Path]) -> None:
        keys_dir = self.server_install_dir / "keys"
        keys_dir.mkdir(parents=True, exist_ok=True)
        for mod_path in mod_paths:
            # Workshop mods usually store keys in a "keys" or "Keys" folder.
            for sub_dir_name in ("keys", "Keys"):
                sub_dir = mod_path / sub_dir_name
                if sub_dir.is_dir():
                    for bikey_file in sub_dir.glob("*.bikey"):
                        try:
                            shutil.copy2(bikey_file, keys_dir / bikey_file.name)
                            print(f"Copied key {bikey_file.name} to {keys_dir}")
                        except Exception as e:
                            print(f"Failed to copy key {bikey_file.name}: {e}")

    def run_steamcmd(
        self,
        steamcmd_args: list[str],
        allow_anonymous_fallback: bool,
    ) -> None:
        attempts = 0
        last_error: Optional[str] = None
        fallback_attempted = False

        while attempts < 2:
            attempts += 1
            success, failure_reason = self._run_steamcmd_once(
                steamcmd_args=steamcmd_args,
                username=self.config["steam"]["username"],
                password=self.config["steam"]["password"],
                guard_code=None,
            )
            if success:
                return

            last_error = failure_reason
            print(f"Steam login attempt {attempts} failed: {failure_reason or 'unknown error'}")

        if allow_anonymous_fallback and not fallback_attempted:
            fallback_attempted = True
            print("Falling back to anonymous Steam login")
            success, failure_reason = self._run_steamcmd_anonymous(steamcmd_args)
            if success:
                return
            last_error = failure_reason

        raise RuntimeError(f"SteamCMD command failed: {last_error or 'unknown error'}")

    def _run_steamcmd_anonymous(self, steamcmd_args: list[str]) -> tuple[bool, Optional[str]]:
        pre_login_lines, post_login_lines = self._partition_steamcmd_script_lines(steamcmd_args)
        script_lines = [
            "@ShutdownOnFailedCommand 1",
            *pre_login_lines,
            "login anonymous",
            *post_login_lines,
            "quit",
        ]
        return self._run_process_with_optional_prompt(script_lines, guard_input=False)

    def _run_steamcmd_once(
        self,
        steamcmd_args: list[str],
        username: str,
        password: str,
        guard_code: Optional[str],
    ) -> tuple[bool, Optional[str]]:
        login_parts = ["login", username, password]
        if guard_code:
            login_parts.append(guard_code)

        pre_login_lines, post_login_lines = self._partition_steamcmd_script_lines(steamcmd_args)
        script_lines = [
            "@ShutdownOnFailedCommand 1",
            *pre_login_lines,
            " ".join(login_parts),
            *post_login_lines,
            "quit",
        ]
        return self._run_process_with_optional_prompt(script_lines, guard_input=True)

    def _run_process_with_optional_prompt(
        self,
        script_lines: list[str],
        guard_input: bool,
    ) -> tuple[bool, Optional[str]]:
        script_content = "\n".join(script_lines) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            dir=self.steamcmd_dir,
            delete=False,
        ) as handle:
            handle.write(script_content)
            script_path = Path(handle.name)

        command = [
            str(self.steamcmd_executable),
            *self.steamcmd_extra_args,
            "+runscript",
            str(script_path),
        ]

        print(f"Running SteamCMD command: {' '.join(command)}")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(self.steamcmd_dir),
        )

        lines: list[str] = []
        queue: Queue[Optional[str]] = Queue()
        console_log_path = self.steamcmd_dir / "logs" / "console_log.txt"
        console_log_offset = console_log_path.stat().st_size if console_log_path.exists() else 0
        streamed_console_lines: set[str] = set()

        def reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                queue.put(line)
            queue.put(None)

        Thread(target=reader, daemon=True).start()

        prompted_for_guard = False
        while True:
            try:
                line = queue.get(timeout=0.2)
            except Empty:
                if guard_input and not prompted_for_guard:
                    log_text, console_log_offset = self._read_console_log_delta(console_log_path, console_log_offset)
                    for log_line in self._iter_displayable_log_lines(log_text, streamed_console_lines):
                        print(log_line)
                    if self._steam_guard_requested(log_text):
                        prompted_for_guard = True
                        code = input("Enter Steam Guard code: ").strip()
                        if process.stdin is not None:
                            process.stdin.write(f"{code}\n")
                            process.stdin.flush()
                if process.poll() is not None and queue.empty():
                    break
                continue

            if line is None:
                break

            lines.append(line)
            print(line, end="")

            normalized = line.strip().lower()
            if (
                guard_input
                and not prompted_for_guard
                and any(marker in normalized for marker in STEAM_GUARD_PROMPTS)
            ):
                prompted_for_guard = True
                code = input("Enter Steam Guard code: ").strip()
                if process.stdin is not None:
                    process.stdin.write(f"{code}\n")
                    process.stdin.flush()

        return_code = process.wait()
        try:
            script_path.unlink()
        except OSError:
            pass
        log_text, console_log_offset = self._read_console_log_delta(console_log_path, console_log_offset)
        for log_line in self._iter_displayable_log_lines(log_text, streamed_console_lines):
            print(log_line)
        output = "".join(lines).lower()

        if return_code == 0 and not any(marker in output for marker in LOGIN_FAILURE_MARKERS):
            return True, None

        failure_reason = self._extract_failure_reason(lines) or f"exit code {return_code}"
        return False, failure_reason

    def _steamcmd_args_to_script_lines(self, steamcmd_args: list[str]) -> list[str]:
        script_lines: list[str] = []
        index = 0
        while index < len(steamcmd_args):
            token = steamcmd_args[index]
            if not token.startswith("+"):
                index += 1
                continue

            parts = [token[1:]]
            index += 1
            while index < len(steamcmd_args) and not steamcmd_args[index].startswith("+"):
                parts.append(steamcmd_args[index])
                index += 1

            script_lines.append(" ".join(parts))

        return script_lines

    def _partition_steamcmd_script_lines(self, steamcmd_args: list[str]) -> tuple[list[str], list[str]]:
        pre_login_commands = {"force_install_dir"}
        pre_login_lines: list[str] = []
        post_login_lines: list[str] = []

        for line in self._steamcmd_args_to_script_lines(steamcmd_args):
            command_name = line.split(maxsplit=1)[0] if line else ""
            if command_name in pre_login_commands:
                pre_login_lines.append(line)
            else:
                post_login_lines.append(line)

        return pre_login_lines, post_login_lines

    def _read_console_log_delta(self, log_path: Path, offset: int) -> tuple[str, int]:
        if not log_path.exists():
            return "", offset

        current_size = log_path.stat().st_size
        if current_size < offset:
            offset = 0

        if current_size == offset:
            return "", offset

        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            text = handle.read()
            return text, handle.tell()

    def _steam_guard_requested(self, text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in STEAM_GUARD_PROMPTS)

    def _iter_displayable_log_lines(self, text: str, seen_lines: set[str]) -> Iterable[str]:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not line.startswith("["):
                continue
            if line in seen_lines:
                continue
            seen_lines.add(line)
            yield line

    def _extract_failure_reason(self, lines: Iterable[str]) -> Optional[str]:
        for line in reversed(list(lines)):
            normalized = line.strip()
            lowered = normalized.lower()
            if any(marker in lowered for marker in LOGIN_FAILURE_MARKERS):
                return normalized
            if "error" in lowered or "failed" in lowered:
                return normalized
        return None

    def build_server_command(self, mod_paths: Iterable[Path]) -> list[str]:
        command = [str(self.server_executable)]
        server_args = self.config["server"].get("args", "")
        if server_args:
            command.extend(shlex.split(server_args, posix=os.name != "nt"))

        mod_paths = [path.resolve() for path in mod_paths if path.exists()]
        if mod_paths:
            mod_arg = ";".join(str(path) for path in mod_paths)
            command.append(f"-mod={mod_arg}")

        return command

    def _now(self) -> datetime:
        return datetime.now(self._scheduler_timezone)

    def _format_datetime(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%d %I:%M:%S %p %Z")

    def _rcon_enabled(self) -> bool:
        return bool(self.rcon_config.get("enabled"))

    def _build_rcon_message_command(self, message: str) -> str:
        template = self.rcon_config.get("message_command", 'say -1 "{message}"')
        return template.format(message=message)

    def _send_rcon_command(self, command: str) -> Optional[str]:
        if not self._rcon_enabled():
            print(f"RCON disabled; skipped command: {command}")
            return None

        host = self.rcon_config.get("host", "127.0.0.1")
        port = int(self.rcon_config.get("port", 2305))
        password = self.rcon_config.get("password", "")
        timeout_seconds = float(self.rcon_config.get("timeout_seconds", 5))
        if not password:
            print("RCON password missing; skipped RCON command")
            return None

        try:
            with self._rcon_lock:
                with BERconClient(host=host, port=port, password=password, timeout_seconds=timeout_seconds) as client:
                    response = client.send_command(command)
                    if response.strip():
                        print(f"RCON response: {response.strip()}")
                    return response
        except Exception as exc:
            print(f"RCON command failed: {exc}")
            return None

    def _send_rcon_message(self, message: str) -> None:
        command = self._build_rcon_message_command(message)
        self._send_rcon_command(command)

    def _check_rcon_connection(self) -> bool:
        if not self._rcon_enabled():
            print("RCON disabled; skipping startup connection check")
            return False

        host = self.rcon_config.get("host", "127.0.0.1")
        port = int(self.rcon_config.get("port", 2305))
        password = self.rcon_config.get("password", "")
        timeout_seconds = float(self.rcon_config.get("timeout_seconds", 5))
        if not password:
            print("RCON password missing; startup connection check skipped")
            return False

        try:
            with self._rcon_lock:
                with BERconClient(host=host, port=port, password=password, timeout_seconds=timeout_seconds):
                    print(f"RCON connection successful ({host}:{port})")
                    return True
        except Exception as exc:
            print(f"RCON connection failed ({host}:{port}): {exc}")
            return False

    def _wait_for_rcon_connection(self) -> bool:
        if not self._rcon_enabled():
            return False

        retry_delay_seconds = int(self.scheduler_config.get("rcon_retry_delay_seconds", 5))
        while not self._stop_requested:
            if self._server_process and self._server_process.poll() is not None:
                print("Server exited before RCON became available")
                return False
            if self._check_rcon_connection():
                return True
            print(f"Retrying RCON connection in {retry_delay_seconds} seconds...")
            for _ in range(retry_delay_seconds):
                if self._stop_requested:
                    return False
                if self._server_process and self._server_process.poll() is not None:
                    print("Server exited before RCON became available")
                    return False
                time.sleep(1)

        return False

    def _parse_clock_time(self, raw_value: str) -> dt_time:
        parts = raw_value.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"Invalid restart time '{raw_value}'. Expected HH:MM or HH:MM:SS.")
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return dt_time(hour=hour, minute=minute, second=second, tzinfo=self._scheduler_timezone)

    def _load_restart_warnings(self) -> list[RestartWarning]:
        warnings: list[RestartWarning] = []
        for item in self.scheduler_config.get("restart_warnings", []):
            offset_minutes = int(item["offset_minutes"])
            warnings.append(
                RestartWarning(
                    offset=timedelta(minutes=offset_minutes),
                    message=str(item["message"]),
                )
            )
        warnings.sort(key=lambda warning: warning.offset, reverse=True)
        return warnings

    def _load_periodic_messages(self, server_started_at: datetime) -> list[PeriodicMessage]:
        periodic_messages: list[PeriodicMessage] = []
        for item in self.scheduler_config.get("periodic_messages", []):
            interval = timedelta(minutes=int(item["interval_minutes"]))
            first_after_start = timedelta(
                minutes=int(item.get("first_after_start_minutes", item["interval_minutes"]))
            )
            periodic_messages.append(
                PeriodicMessage(
                    interval=interval,
                    message=str(item["message"]),
                    first_after_start=first_after_start,
                    next_run_at=server_started_at + first_after_start,
                )
            )
        return periodic_messages

    def _next_restart_time(self, now: datetime) -> Optional[datetime]:
        restart_times = self.scheduler_config.get("restart_times", [])
        if not restart_times:
            return None

        candidates: list[datetime] = []
        for raw_value in restart_times:
            scheduled_time = self._parse_clock_time(str(raw_value))
            candidate = now.replace(
                hour=scheduled_time.hour,
                minute=scheduled_time.minute,
                second=scheduled_time.second,
                microsecond=0,
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            candidates.append(candidate)

        return min(candidates) if candidates else None

    def _build_runtime_schedule(self, server_started_at: datetime) -> RuntimeSchedule:
        next_restart_at = self._next_restart_time(server_started_at)
        if next_restart_at:
            print(f"Next scheduled restart at {self._format_datetime(next_restart_at)}")
        return RuntimeSchedule(
            next_restart_at=next_restart_at,
            sent_warning_offsets=set(),
            periodic_messages=self._load_periodic_messages(server_started_at),
        )

    def _dispatch_scheduler_actions(self, schedule: RuntimeSchedule, now: datetime) -> bool:
        for periodic in schedule.periodic_messages:
            while periodic.next_run_at and now >= periodic.next_run_at:
                print(f"Sending periodic message: {periodic.message}")
                self._send_rcon_message(periodic.message)
                periodic.next_run_at += periodic.interval

        if not schedule.next_restart_at:
            return False

        for warning in self._load_restart_warnings():
            offset_seconds = int(warning.offset.total_seconds())
            if offset_seconds in schedule.sent_warning_offsets:
                continue
            if now >= schedule.next_restart_at - warning.offset:
                print(f"Sending restart warning: {warning.message}")
                self._send_rcon_message(warning.message)
                schedule.sent_warning_offsets.add(offset_seconds)

        if now >= schedule.next_restart_at:
            print("Scheduled restart time reached")
            return True

        return False

    def _restart_server_process(self, reason: str) -> None:
        if not self._server_process or self._server_process.poll() is not None:
            return

        restart_command = self.scheduler_config.get("restart_command")
        if restart_command:
            print(f"Sending pre-restart RCON command: {restart_command}")
            self._send_rcon_command(str(restart_command))

        shutdown_grace_seconds = int(self.scheduler_config.get("shutdown_grace_seconds", 15))
        print(f"Stopping server for {reason}")
        self._server_process.terminate()
        try:
            self._server_process.wait(timeout=shutdown_grace_seconds)
        except subprocess.TimeoutExpired:
            print("Server did not stop in time; killing process")
            self._server_process.kill()
            self._server_process.wait(timeout=10)

    def _start_rcon_console(self) -> None:
        if not self._rcon_enabled():
            return
        if not self.scheduler_config.get("interactive_rcon_console", False):
            return
        if self._console_thread and self._console_thread.is_alive():
            return

        self._console_stop_requested = False
        self._console_thread = Thread(target=self._rcon_console_loop, daemon=True)
        self._console_thread.start()
        print("Interactive RCON console enabled. Type commands and press Enter. Type 'exit' to stop the manager.")

    def _stop_rcon_console(self) -> None:
        self._console_stop_requested = True

    def _rcon_console_loop(self) -> None:
        while not self._console_stop_requested and not self._stop_requested:
            try:
                raw_command = input("rcon> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                self.stop()
                break

            command = raw_command.strip()
            if not command:
                continue
            if command.lower() in {"exit", "quit"}:
                print("Stopping manager from interactive RCON console")
                self.stop()
                break

            self._send_rcon_command(command)

    def start_and_monitor_server(self, mod_paths: Iterable[Path]) -> None:
        self._copy_mod_keys(mod_paths)
        restart_delay = int(self.config["server"].get("restart_delay", 10))
        command = self.build_server_command(mod_paths)

        while not self._stop_requested:
            if not self.server_executable.exists():
                raise FileNotFoundError(f"Server executable not found: {self.server_executable}")

            print(f"Starting DayZ server: {' '.join(command)}")
            self._server_process = subprocess.Popen(command)
            rcon_startup_delay = int(self.scheduler_config.get("rcon_startup_delay_seconds", 15))
            if rcon_startup_delay > 0:
                for _ in range(rcon_startup_delay):
                    if self._stop_requested:
                        break
                    if self._server_process.poll() is not None:
                        break
                    time.sleep(1)
            if self._wait_for_rcon_connection():
                self._start_rcon_console()
            schedule = self._build_runtime_schedule(self._now())
            scheduled_restart_triggered = False

            while not self._stop_requested:
                return_code = self._server_process.poll()
                if return_code is not None:
                    break

                if self._dispatch_scheduler_actions(schedule, self._now()):
                    scheduled_restart_triggered = True
                    self._restart_server_process("scheduled restart")
                    return_code = self._server_process.wait()
                    break

                time.sleep(1)

            self._server_process = None
            self._stop_rcon_console()

            if self._stop_requested:
                break

            restart_reason = "scheduled restart" if scheduled_restart_triggered else f"exit code {return_code}"
            print(f"Server stopped due to {restart_reason}. Restarting in {restart_delay} seconds...")
            time.sleep(restart_delay)

    def stop(self) -> None:
        self._stop_requested = True
        self._stop_rcon_console()
        if self._server_process and self._server_process.poll() is None:
            self._restart_server_process("manager shutdown")

    def run(self) -> None:
        self.ensure_steamcmd()
        self.install_or_update_server()
        mod_paths = self.update_mods()
        print(f"Prepared {len(mod_paths)} mod(s)")
        self.start_and_monitor_server(mod_paths)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automated DayZ dedicated server manager")
    parser.add_argument(
        "-c",
        "--config",
        default="dayz_server_manager.yaml",
        help="Path to the YAML configuration file",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manager = DayZServerManager(Path(args.config))

    def handle_signal(signum: int, _frame: object) -> None:
        print(f"Received signal {signum}, shutting down")
        manager.stop()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    try:
        manager.run()
        return 0
    except KeyboardInterrupt:
        manager.stop()
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
