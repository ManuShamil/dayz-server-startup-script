# DayZ Server Manager -- Python Script Specification (v3)

## Overview

Build a Python-based server manager that:

1.  Installs SteamCMD if not present
2.  Authenticates with Steam (with Steam Guard support)
3.  Downloads or updates the DayZ dedicated server
4.  Downloads/updates Workshop mods
5.  Starts the DayZ server
6.  Monitors and restarts the server if it crashes

------------------------------------------------------------------------

## Tech Requirements

-   Python 3.10+
-   Dependencies:
    -   pyyaml
    -   requests

------------------------------------------------------------------------

## Configuration File (YAML)

``` yaml
steamcmd:
  path: "./steamcmd"

steam:
  username: "your_username"
  password: "your_password"

server:
  install_dir: "./dayzserver"
  executable: "./dayzserver/DayZServer_x64"
  args: "-config=serverDZ.cfg -port=2302"
  restart_delay: 10

mods:
  workshop_app_id: 221100
  directory: "./dayzserver/steamapps/workshop/content/221100"
  links:
    - "https://steamcommunity.com/sharedfiles/filedetails/?id=1559212036"
```

------------------------------------------------------------------------

## Steam Guard Handling

SteamCMD may request a Steam Guard code.

Requirements:

-   Detect when SteamCMD prompts for Steam Guard

-   Prompt user in CLI: "Enter Steam Guard code:"

-   Pass code to SteamCMD login: +login username password
    `<guard_code>`{=html}

-   If login fails:

    -   Retry once
    -   Then fallback to anonymous login (optional)

------------------------------------------------------------------------

## SteamCMD Installation

-   Auto-download if not present
-   Linux: tar.gz
-   Windows: zip
-   Extract and set executable permissions

------------------------------------------------------------------------

## Server Installation

Command:

+force_install_dir
```{=html}
<dir>
```
+app_update 223350 validate

------------------------------------------------------------------------

## Mod Updates

Command:

+workshop_download_item 221100 `<mod_id>`{=html} validate

------------------------------------------------------------------------

## Server Execution

\<server.executable\> `<args>`{=html} -mod=`<paths>`{=html}

------------------------------------------------------------------------

## Monitoring Loop

-   Restart on crash
-   Delay configurable

------------------------------------------------------------------------

## Goal

A fully automated, self-healing DayZ server manager
