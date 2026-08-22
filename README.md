# ServerDeck

**ServerDeck** is a lightweight, self-contained web interface for managing Debian, Ubuntu and Raspberry Pi OS servers.

It is designed to make common Linux server administration tasks easier without requiring a heavy management stack. ServerDeck uses the tools already provided by the operating system wherever possible — including APT, systemd, rsync, NetworkManager, SMART, UFW, Docker and `/proc`/`/sys` — while presenting them through a simple guided interface.

ServerDeck v1 focuses on being **small, understandable and low-overhead**, with monitoring performed on demand or at sensible intervals rather than through permanent metrics collectors.

## Supported Systems

- Debian
- Ubuntu Server
- Raspberry Pi OS

ServerDeck supports common x86-64 and ARM systems. Individual Docker applications may have their own architecture requirements.

## Latest Release

> **Current release:** v1.30.0 — Final Release  
> ServerDeck v1 is feature complete. Future development continues with ServerDeck v2.

To get started and download the latest release type:

```bash
wget https://github.com/thefistedpigeon/ServerDeck/releases/download/ServerDeck-v1.30.0-Final/serverdeck-v1.30.0.py
```
## Installation

Once the ServerDeck script has been downloaded to the server:

```bash
sudo chmod +x ./serverdeck-v1.30.0.py
```

Install the service:

```bash
sudo ./serverdeck-v1.30.0.py --install-service
```

ServerDeck shouls start automatically. 

ServerDeck uses port `9090` by default.

Open:

```text
http://SERVER-IP:9090
```

A custom port can be selected when installing the service:

```bash
sudo ./serverdeck-v1.30.0.py --install-service -port 8081
```

## ServerDeck v1

**v1.30.0 is the final planned ServerDeck v1 release.**

The v1 branch will remain available for users who want the established lightweight ServerDeck experience, while future development moves to ServerDeck v2.

## Features

### Overview

- CPU, memory, swap, storage and network activity
- Hostname management
- Installed managed-service status
- Disk SMART health and temperature information
- TuneD power/performance profile management
- Recent ServerDeck activity
- Restart-required and restart-recommended notifications
- Shutdown and restart controls

### Updates

- Check for available APT updates
- Install security or all available updates
- Full system upgrade
- Autoremove and autoclean
- Live operation output
- Restart-required awareness after relevant system changes

### Backups

- Guided rsync backup creation
- Source and destination browser
- Common rsync options
- Required dry-run before saving a backup
- On-demand or scheduled backups
- systemd-backed scheduling
- Backup history, duration and result tracking
- Failed and overdue backup warnings

### Disks

- View physical disks and partitions
- Create, delete, extend and shrink partitions
- Format FAT, exFAT, NTFS, ext4, XFS and Btrfs filesystems
- Mount and unmount filesystems
- Persistent mount configuration
- Change permissions on mounted non-system storage
- SMART health information
- Disk temperature and power-on hours
- Connection/protocol information
- Protection for system disks and filesystems

### Network

- View network interfaces and addresses
- Configure IP settings
- Scan for and connect to Wi-Fi networks
- UFW firewall installation and management
- View listening/exposed TCP and UDP services
- Create LAN, custom-network or public firewall rules

### Users & Groups

- View and manage local users
- Create and manage groups
- User and group membership management

### Processes

- Live process list
- Search, sort and filter processes
- Process details
- Stop or kill processes
- Compact internally scrollable desktop view

### Services

- View installed systemd services
- Search, filter and inspect service state
- Start, stop and restart services
- View triggers and schedules
- Create and remove ServerDeck-managed services
- Compact internally scrollable desktop view

### CopyParty

- Guided CopyParty download and setup
- Dedicated service user and group
- Shared-folder selection and permission setup
- CopyParty user and folder configuration
- Optional thumbnail dependencies
- Start, stop, restart and uninstall controls

### Media / MiniDLNA

- Guided MiniDLNA installation
- Friendly server name
- Interface and port configuration
- Multiple media directories
- Media type selection
- Folder browser and permission checks
- inotify support
- Start, stop, restart and uninstall controls

### Docker

- Guided Docker Engine installation and removal
- View and manage containers
- Pull and manage images
- Create containers through a guided workflow
- Volumes and environment variables
- Port mapping
- Docker network management
- Dedicated container IP addresses using MACVLAN or IPVLAN
- Architecture and configuration preflight checks
- Safe container update/recreate workflow
- Interactive container shell
- Image and volume cleanup tools
- Existing configuration review before deployment

#### Docker App Library

ServerDeck includes a curated App Library with guided setup for:

- AdGuard Home
- Immich
- Syncthing
- Nextcloud
- Jellyfin
- Vaultwarden

Recipes provide sensible defaults while still allowing the final Docker configuration to be reviewed before deployment.

### Terminal

- Persistent web shell
- Command history
- Click history entries to recall commands
- Keyboard Up/Down history navigation
- Delete individual or all history entries
- Interrupt running commands
- Clear, restart or end the shell session
- Full commands shown when hovering over truncated history entries

### Needs Attention & Notifications

ServerDeck can surface important conditions including:

- Restart required or recommended
- High CPU temperature
- Critical filesystem usage
- SMART disk warnings
- Failed or overdue backups
- Enabled managed services that stop unexpectedly

Optional notifications can be sent through:

- Generic webhooks
- ntfy

Checks are intentionally lightweight and do not require a permanent monitoring stack.

### Interface

- Dark and light themes
- OS-aware navigation accents:
  - Ubuntu — orange
  - Debian — red
  - Raspberry Pi OS — red
  - Other distributions — ServerDeck blue
- Responsive desktop/mobile layout
- Hostname-first browser tab titles
- Ctrl+K command/search palette
- Consistent guided installation workflows for managed applications

## Design Philosophy

ServerDeck deliberately avoids heavy dependencies and always-on monitoring systems. It aims to provide the most useful day-to-day server management tasks while staying small enough that the management interface itself has negligible impact on the server.

Where possible, ServerDeck manages the underlying Linux tools rather than replacing them.
