# DayZ Server Manager

A Python-based DayZ dedicated server manager that installs SteamCMD, updates the server and Workshop mods, starts the server, monitors it, schedules restarts, sends restart announcements through BattlEye RCON, and optionally gives you an interactive RCON console in the same terminal.

## Features

- Installs SteamCMD automatically if it is missing
- Supports Steam login with interactive Steam Guard code entry
- Installs or updates the DayZ dedicated server through SteamCMD
- Downloads and updates Workshop mods from Steam links
- Starts the DayZ server with your configured launch arguments
- Restarts the server automatically after crashes
- Schedules restarts from YAML-defined daily times
- Sends YAML-driven periodic announcements through BattlEye RCON
- Sends restart warning messages before scheduled restarts
- Retries RCON connection until it becomes available
- Provides an optional interactive `rcon>` prompt for manual commands

## Requirements

- Python 3.10+
- Windows or Linux
- A Steam account that can download the DayZ dedicated server
- BattlEye RCON enabled on the server if you want scheduled announcements or the interactive RCON console

## Installation

1. Clone the repository.
2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

3. Copy the example config:

```bash
cp dayz_server_manager.yaml.example dayz_server_manager.yaml
```

On Windows PowerShell:

```powershell
Copy-Item dayz_server_manager.yaml.example dayz_server_manager.yaml
```

4. Edit `dayz_server_manager.yaml` with your Steam credentials, DayZ paths, mod links, and RCON settings.

## Quick Start

Run the manager with the default config file:

```bash
python dayz_server_manager.py
```

Or pass a custom config path:

```bash
python dayz_server_manager.py --config path/to/dayz_server_manager.yaml
```

On first run, the manager will:

1. Download and extract SteamCMD if needed
2. Ask for a Steam Guard code if Steam requests one
3. Install or update the DayZ dedicated server
4. Download or update configured Workshop mods
5. Start the DayZ server
6. Wait for BattlEye RCON, if enabled
7. Begin monitoring, scheduled announcements, and automatic restarts

## Configuration

The project ships with a safe example file at `dayz_server_manager.yaml.example`.

The real `dayz_server_manager.yaml` is ignored by Git so you can keep credentials and server secrets local.

### Example

```yaml
steamcmd:
  path: "./steamcmd"

steam:
  username: "your_steam_username"
  password: "your_steam_password"

server:
  install_dir: "./dayzserver"
  executable: "./dayzserver/DayZServer_x64.exe"
  args: "-config=serverDZ.cfg -port=2302 -profiles=profiles -dologs -adminlog -netlog"
  restart_delay: 10

mods:
  workshop_app_id: 221100
  directory: "./dayzserver/steamapps/workshop/content/221100"
  links:
    - "https://steamcommunity.com/sharedfiles/filedetails/?id=1559212036"
    - "https://steamcommunity.com/sharedfiles/filedetails/?id=1564026768"

rcon:
  enabled: true
  host: "127.0.0.1"
  port: 2305
  password: "your_battleye_rcon_password"
  message_command: 'say -1 "{message}"'
  timeout_seconds: 5

scheduler:
  timezone: "Asia/Kolkata"
  restart_times:
    - "00:00"
    - "04:00"
    - "08:00"
    - "12:00"
    - "16:00"
    - "20:00"
  restart_warnings:
    - offset_minutes: 60
      message: "Scheduled restart in 60 minutes."
    - offset_minutes: 45
      message: "Scheduled restart in 45 minutes."
    - offset_minutes: 30
      message: "Scheduled restart in 30 minutes."
    - offset_minutes: 15
      message: "Scheduled restart in 15 minutes."
    - offset_minutes: 10
      message: "Scheduled restart in 10 minutes."
    - offset_minutes: 5
      message: "Scheduled restart in 5 minutes."
    - offset_minutes: 3
      message: "Scheduled restart in 3 minutes."
    - offset_minutes: 1
      message: "Scheduled restart in 1 minute."
  periodic_messages:
    - interval_minutes: 60
      first_after_start_minutes: 60
      message: "Remember to take breaks and keep your gear safe."
  rcon_startup_delay_seconds: 15
  rcon_retry_delay_seconds: 5
  interactive_rcon_console: true
  shutdown_grace_seconds: 15
```

### Config Sections

`steamcmd`

- `path`: Where SteamCMD should be installed

`steam`

- `username`: Steam username used for SteamCMD login
- `password`: Steam password used for SteamCMD login

`server`

- `install_dir`: DayZ dedicated server install path
- `executable`: Server executable path
- `args`: Launch arguments passed to the server
- `restart_delay`: Delay in seconds before restarting after a crash or planned shutdown

`mods`

- `workshop_app_id`: Steam Workshop app ID, usually `221100` for DayZ
- `directory`: Configured Workshop content directory
- `links`: List of Steam Workshop item URLs

`rcon`

- `enabled`: Enables BattlEye RCON integration
- `host`: RCON host, usually `127.0.0.1` when the manager runs on the same machine
- `port`: BattlEye RCON port
- `password`: BattlEye RCON password
- `message_command`: Template used for announcement messages
- `timeout_seconds`: UDP socket timeout for each RCON attempt

`scheduler`

- `timezone`: Time zone used for `restart_times`
- `restart_times`: Daily restart times in `HH:MM` or `HH:MM:SS`
- `restart_warnings`: Messages to send before a scheduled restart
- `periodic_messages`: Messages sent on a repeating interval while the server is running
- `rcon_startup_delay_seconds`: Wait before first RCON connection attempt after server launch
- `rcon_retry_delay_seconds`: Delay between failed RCON connection attempts
- `interactive_rcon_console`: Enables the interactive `rcon>` terminal
- `shutdown_grace_seconds`: How long to wait after terminate before force-killing the server process

## BattlEye RCON Setup

To use announcements or the interactive console, your DayZ/BattlEye setup must expose RCON with matching values for port and password.

Typical BattlEye config values look like:

```text
RConPassword your_password_here
RConPort 2305
RestrictRcon 0
```

Make sure the values in your DayZ/BattlEye configuration match the `rcon` section in `dayz_server_manager.yaml`.

If RCON is working, startup output should eventually show:

```text
RCON connection successful (127.0.0.1:2305)
```

If it is not working, the manager will keep retrying and print the failure reason.

## Interactive RCON Console

When `interactive_rcon_console: true` and RCON connects successfully, the manager starts a local prompt:

```text
rcon>
```

You can type raw BattlEye RCON commands directly there, for example:

```text
rcon> players
rcon> say -1 "Server restart in 5 minutes"
```

Typing `exit` or `quit` stops the manager.

## Scheduled Restarts and Messages

Scheduled restarts are driven by `scheduler.restart_times`.

The manager:

- Sends warning messages at the offsets listed in `restart_warnings`
- Stops the server when the restart time is reached
- Waits `server.restart_delay`
- Starts it again
- Reconnects to RCON and resumes scheduling

Periodic messages are independent of restart warnings. They repeat according to `interval_minutes`, starting after `first_after_start_minutes`.

## Security Notes

- Do not commit `dayz_server_manager.yaml`
- Treat Steam credentials and RCON passwords as secrets
- If a password has been pasted into terminal logs, screenshots, or chat history, rotate it

## Troubleshooting

### SteamCMD hangs after printing its banner

The manager watches SteamCMD logs and should prompt for Steam Guard when needed. If Steam asks for email verification, enter the code when prompted in the terminal.

### RCON keeps timing out

Check:

- `rcon.host`, `rcon.port`, and `rcon.password`
- BattlEye RCON is enabled on the server
- Windows Firewall or host firewall allows UDP on the RCON port
- The official BattlEye RCON client can connect with the same settings

### Time zone error on Windows

Use a valid IANA time zone such as `Asia/Kolkata`. The manager also maps `Asia/Calcutta` to `Asia/Kolkata`.

## Development

Quick syntax validation:

```bash
python -m py_compile dayz_server_manager.py
```

## License

Add your preferred open-source license here.
