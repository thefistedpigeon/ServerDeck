# ServerDeck

ServerDeck is a lightweight, self-contained web interface for managing Debian, Ubuntu and Raspberry Pi OS servers.

The goal is to provide the most useful day-to-day server administration tools in a simple interface without becoming a large, resource-heavy control panel. ServerDeck is designed primarily for home servers, homelabs and small systems where a full platform may be more than is needed.

ServerDeck is distributed as a single Python script and uses standard Linux tools wherever possible.

ServerDeck has been written with the help of AI and has been tested on a Raspberry Pi 5 and Raspberry Pi 3b both running Raspberry Pi OS Lite (64bit and 32Bit respectively) as well as a fresh Ubuntu server install. I created it as an alternative to some of the other WebUI's available with a view to be simple and easy to use. It is built around using software like CopyParty and Docker/Portainer in unison rather than to be a "does everything" application.

## Latest Release

To get started and download the latest release type:

```bash
wget https://github.com/thefistedpigeon/ServerDeck/releases/download/ServerDeck-v1.18.0/serverdeck-v1.18.0.py
```
## Installation

Once the ServerDeck script has been downloaded to the server:

```bash
sudo chmod +x ./serverdeck-v1.14.1.py
```

Install the service:

```bash
sudo serverdeck-v1.14.1.py --install-service
```

ServerDeck shouls start automatically. 

ServerDeck uses port `9090` by default.

Open:

```text
http://SERVER-IP:9090
```

A custom port can be selected when installing the service:

```bash
sudo serverdeck-v1.14.1.py --install-service -port 8081
```

## Features

### Overview
- CPU, memory, storage and network activity
- Hostname and uptime
- Quick hostname rename
- Recent ServerDeck activity
- Service status for CopyParty and Docker
- Restart and shutdown controls

### Updates
- View available APT package updates
- Refresh package lists
- Install all updates
- Install security updates only
- Full system upgrade
- Autoclean and autoremove

### Disks
- View disks, filesystems and mount points
- Mount and unmount existing filesystems
- Create persistent UUID-based mounts
- Optional `noatime` and systemd automount configuration
- SMART health information
- Install `smartmontools` when required

ServerDeck does not provide disk formatting or partitioning.

### Network
- View network interfaces, addresses, gateways, DNS and MTU
- Configure DHCP or static IPv4
- NetworkManager and Netplan support
- Automatic rollback protection when changing static IP settings
- Wi-Fi scanning and connection management
- Open, WPA/WPA2 Personal and WPA3/SAE network support

### Users & Groups
- Create and manage local Linux users
- Change passwords
- Lock and unlock accounts
- Manage shells, home directories and supplementary groups
- Manage SSH public keys
- Create and manage local groups
- Protections for root, system accounts and critical groups

### Processes
- View running processes
- PID, user, CPU, memory, state, runtime and command details
- Search, filter and sorting
- Process detail view
- Send SIGTERM or SIGKILL
- Protections for critical ServerDeck/system processes

### Services
- View systemd services and their current state
- Start, stop and restart services
- Enable or disable services at boot
- View activation source and unit details
- Create simple custom systemd services
- Favourite commonly used services
- Safely remove eligible ServerDeck-created units

### Backups
Guided backup creation using `rsync`:

1. Select a source folder
2. Select a destination
3. Choose backup options
4. Perform a mandatory dry run
5. Choose on-demand or scheduled operation
6. Create the backup service

Additional features:
- Local and remote rsync destinations
- On-demand backups
- systemd timer or cron scheduling
- Backup logs
- Safe source/destination checks
- Existing backups remain manageable through a simple dashboard

### CopyParty
Integrated CopyParty setup and management with a guided installation process:

1. Download CopyParty
2. Configure the dedicated service account and storage
3. Create CopyParty accounts and shared folders
4. Select optional features
5. Review the configuration and install the service

ServerDeck can also:
- Run CopyParty as a dedicated `copyparty` system account
- Manage multiple shared folders
- Manage CopyParty users and groups
- Configure read/write/upload access
- Enable thumbnails, LAN discovery and temporary shares
- Optionally enable search/indexing and media metadata
- Check filesystem permissions
- Manage the CopyParty systemd service
- Reset ServerDeck's CopyParty configuration with Fresh Start without deleting shared files

### Docker & Portainer
- Detect Docker installation and service state
- Install Docker Engine using the official Docker APT repository
- Start, stop and restart Docker
- Show basic Docker installation information
- Install and manage Portainer CE
- Create the Portainer data volume automatically
- Open Portainer directly from ServerDeck

### Terminal
- Browser-based persistent shell
- Runs as the signed-in Linux user
- Persistent working directory
- Command history
- Interrupt, clear and restart controls

The built-in terminal is intended for normal shell commands. For full-screen terminal applications such as `nano`, `vim`, `top` or `htop`, SSH is recommended.

## Authentication

ServerDeck can authenticate against local Linux accounts using PAM.

By default, access can be restricted to members of an authorised administrative group such as `sudo`.

ServerDeck itself normally runs as root so that it can perform system administration tasks, while user shell sessions run as the signed-in Linux account.

## Supported Systems

ServerDeck is intended for Debian-derived systems, including:

- Debian
- Ubuntu Server
- Raspberry Pi OS

Some functionality depends on standard system utilities such as systemd, APT, NetworkManager, Netplan, rsync, smartctl or Docker.

## Security

ServerDeck is a server administration interface and should be treated accordingly.

Recommended deployment:
- Trusted local network
- VPN
- HTTPS reverse proxy
- Strong Linux account passwords
- Appropriate firewall rules

The built-in web server uses HTTP. For access beyond a trusted network, place ServerDeck behind a properly configured HTTPS reverse proxy or VPN.

ServerDeck avoids storing passwords where possible and excludes sensitive credentials from configuration exports and activity history.

## Project Goals

ServerDeck aims to be:

- **Simple** — common server tasks should be easy to find and understand.
- **Lightweight** — avoid unnecessary dependencies, background services and polling.
- **Safe** — destructive actions should be explicit and protected.
- **Practical** — use standard Linux tools rather than reinventing them.
- **Self-contained** — keep deployment and upgrades straightforward.
- **Focused** — provide useful server management without trying to replace every specialised administration tool.

## License

GNU GPL
