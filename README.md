# ServerDeck

**A lightweight, self-hosted web interface for managing Debian and Ubuntu servers.**

ServerDeck is a single-file Python application designed to make common server-management tasks accessible through a clean web interface without requiring a large management stack or Python web framework.

It was created as a smaller, more focused alternative to tools such as Cockpit, with an emphasis on:

- Simple installation
- Minimal dependencies
- Familiar Linux account authentication
- Clear, task-focused controls
- Safe defaults for less technical users
- A single Python script that can be copied to a new server and run

ServerDeck is designed primarily for Debian and Ubuntu systems and is also suitable for Debian-derived systems such as Raspberry Pi OS where the required system tools are available.

> **Current development release:** v0.9.7  
> ServerDeck is currently undergoing final clean-system testing ahead of a planned v1.0.0 release.

---

## Navigation

The current ServerDeck navigation is:

```text
Overview | Updates | rSync | Disks | Network | CopyParty | Terminal
```

The **Terminal** page intentionally remains the final page in the navigation.

A persistent **Power Options** button is available from every page.

---

## Features

### Overview

The Overview page provides an at-a-glance view of the server.

It includes:

- System hostname
- Operating-system information
- Server uptime
- System load averages
- CPU usage
- Memory usage
- Root filesystem storage usage
- Current network download speed
- Current network upload speed
- Total network data received
- Total network data transmitted
- Active network interfaces included in traffic statistics
- Restart-required status

The hostname can be changed directly from the web interface.

Network activity is calculated from Linux network counters and excludes the loopback interface.

---

### Updates

The Updates page provides a graphical interface for Debian and Ubuntu APT package management.

It can:

- Refresh the APT package lists
- List available package updates
- Display installed and candidate package versions
- Identify available security updates
- Install only security updates
- Install standard updates
- Perform a full upgrade
- Run APT autoclean and autoremove
- Display live command output
- Report when the system recommends a restart

Available actions include:

#### Only install security updates

Installs packages whose current APT candidate originates from a Debian or Ubuntu security repository.

#### Install all updates

Runs the equivalent of a standard:

```bash
sudo apt update
sudo apt upgrade -y
```

#### Full Upgrade

Runs the equivalent of:

```bash
sudo apt update
sudo apt full-upgrade -y
```

A full upgrade may install new packages or remove packages when required to resolve dependency changes, so ServerDeck displays an additional confirmation warning before running it.

#### Autoclean & Autoremove

Runs the equivalent of:

```bash
sudo apt autoclean
sudo apt autoremove -y
```

ServerDeck internally uses `apt-get` for non-interactive package operations.

---

### rSync

The rSync page provides a graphical interface for creating, running and scheduling rsync jobs.

It supports:

- Local source paths
- Local destination paths
- Remote rsync destinations
- Manual path entry
- Server-side folder browsing
- Immediate job execution
- Scheduled jobs using systemd timers or cron
- Persistent job logs
- Editing existing jobs
- Enabling and disabling schedules
- Deleting jobs
- Installing `rsync` from the web interface if it is not already installed

Supported rsync options include:

- `--dry-run`
- `--archive`
- `--itemize-changes`
- `--verbose`
- `--human-readable`
- `-P`
- `--update`
- `--chmod=`

ServerDeck stores backup-job configuration and logs under:

```text
/var/lib/serverdeck/
```

when installed as a system service.

---

### Disks

The Disks page provides basic management of existing filesystems and removable storage.

It can:

- List detected block filesystems
- Show filesystem size and type
- Display mount status
- Display connection type
- Show filesystem labels
- Mount filesystems temporarily
- Select a mount folder using the server-side folder browser
- Manually enter a mount folder
- Create the final mount directory when necessary
- Create persistent mounts that survive restarts
- Unmount filesystems
- Remove ServerDeck-managed persistent mount entries

Persistent mounts are written to:

```text
/etc/fstab
```

using filesystem UUIDs instead of device names such as `/dev/sda1`.

Optional persistent-mount settings include:

- `noatime`
- systemd automount
- Automatic mounting after startup

ServerDeck deliberately does **not** provide disk formatting or partitioning.

For safety it also refuses to modify the detected system disk and only removes `/etc/fstab` entries originally created by ServerDeck.

---

### Network

The Network page displays current interface information and allows basic IPv4 configuration.

Interface information includes:

- Interface name
- Connection state
- Current IPv4 address
- Gateway
- DNS servers
- DHCP/static method
- MAC address
- MTU
- Active connection profile where available

ServerDeck can configure:

- Automatic IPv4 using DHCP
- Static IPv4 address using CIDR notation
- Default gateway
- DNS servers

Example static address:

```text
192.168.1.50/24
```

ServerDeck uses:

- **NetworkManager** when it is active
- A ServerDeck-managed **Netplan** override when Netplan is available
- Read-only mode when neither supported configuration backend is available

#### Static-IP rollback protection

Changing an active server IP can disconnect both the browser and SSH.

To reduce the chance of accidentally locking yourself out, static-IP changes use a confirmation system:

1. ServerDeck applies the requested address.
2. The browser attempts to reconnect using the new IP.
3. The new configuration must be confirmed.
4. If confirmation is not received within 90 seconds, ServerDeck attempts to restore the previous network configuration.

Network changes should still be tested carefully, preferably with local console access available.

---

### CopyParty

ServerDeck includes optional integration with [CopyParty](https://github.com/9001/copyparty), a standalone file-sharing server.

The CopyParty page can:

- Download the latest official `copyparty-sfx.py` release
- Update an existing downloaded CopyParty script
- Preserve the previous downloaded script when updating
- Install optional thumbnail dependencies
- Select a shared folder using the server-side folder browser
- Install a persistent `copyparty.service`
- Enable CopyParty automatically at system startup
- Start the CopyParty service
- Stop the CopyParty service
- Restart the CopyParty service
- View recent CopyParty service logs
- Open the running CopyParty interface
- Delete the ServerDeck-managed CopyParty service

The CopyParty script is downloaded to:

```text
/opt/copyparty/copyparty-sfx.py
```

The official release URL used by ServerDeck is:

```text
https://github.com/9001/copyparty/releases/latest/download/copyparty-sfx.py
```

When updating the downloaded script, the previous copy is retained as:

```text
/opt/copyparty/copyparty-sfx.py.previous
```

#### Thumbnail support

ServerDeck can install:

```bash
sudo apt install --no-install-recommends python3-pil ffmpeg
```

for CopyParty image/video thumbnail support.

#### CopyParty system service

The user chooses an existing folder to expose through CopyParty.

ServerDeck then creates:

```text
/etc/systemd/system/copyparty.service
```

using a command similar to:

```bash
/opt/copyparty/copyparty-sfx.py -v "/path/to/folder::rw" -z
```

The service:

- Starts immediately
- Is enabled automatically at boot
- Runs as the Linux account that configured it through ServerDeck
- Does not run as root
- Uses the selected folder as the CopyParty web root

CopyParty normally listens on port:

```text
3923
```

#### Important CopyParty security note

The current simple ServerDeck CopyParty configuration exposes the selected folder with **anonymous read/write access**.

Anyone who can reach the CopyParty service can read files from and upload files to that folder.

Only use this configuration on networks where that access is appropriate.

Deleting the CopyParty service from ServerDeck removes the managed systemd service but does **not** delete:

- The downloaded CopyParty script
- The selected shared folder
- Files stored inside that folder

---

### Terminal

ServerDeck includes a browser-based command terminal.

The terminal:

- Runs as the Linux account used to sign in
- Uses a persistent shell session
- Preserves working-directory changes such as `cd`
- Displays command output in real time
- Supports command history with the Up and Down arrow keys
- Allows running commands to be interrupted
- Allows the shell session to be cleared
- Allows the shell to be restarted
- Allows the shell session to be ended manually

Terminal sessions are held in memory.

The web terminal is intended for normal command-line tasks. Full-screen interactive applications such as:

- `nano`
- `vim`
- `top`
- `htop`

are better used through SSH.

Terminal access should be treated as equivalent to SSH access for the signed-in account.

---

### Power Options

A persistent **Power Options** control is available from every ServerDeck page.

It provides:

- Restart server
- Shut down server
- Confirmation prompts before either action

Power actions are protected by the authenticated ServerDeck session and CSRF validation.

Active:

- SSH sessions
- Terminal commands
- Update operations
- rSync jobs

may be interrupted when the server is restarted or shut down.

---

## Authentication

By default, ServerDeck authenticates users against the server's local Linux accounts using PAM.

This means the same local username and password used for SSH can be used to sign in to the ServerDeck web interface.

By default, access is restricted to members of the local:

```text
sudo
```

group.

This prevents ordinary local users from gaining access to ServerDeck's administrative functions.

Passwords are passed to Linux PAM for validation and are not stored by ServerDeck.

### SSH key users

SSH private keys cannot be entered into the ServerDeck login page.

If an account normally uses key-only SSH authentication, it must also have a Linux password configured to use the web login.

For example:

```bash
sudo passwd yourusername
```

### Alternative authentication modes

ServerDeck also retains optional static-password and no-auth modes for specialist/testing use.

Authentication should **not** be disabled on an untrusted network.

---

## Installation

ServerDeck requires Python 3 and is designed to run without an external Python web framework.

Make the downloaded script executable:

```bash
chmod +x serverdeck.py
```

### Install using the default port

Install ServerDeck as a systemd service:

```bash
sudo ./serverdeck.py --install-service
```

The default web port is:

```text
9090
```

Then open:

```text
http://SERVER-IP:9090/
```

### Install using a custom port

A custom port can be selected during service installation.

For example:

```bash
sudo ./serverdeck.py --install-service -port 8081
```

The conventional long form is also supported:

```bash
sudo ./serverdeck.py --install-service --port 8081
```

The selected port is stored in the generated systemd service and remains in use after restarts and reboots.

Valid ports are between `1` and `65535`.

Example:

```text
http://SERVER-IP:8081/
```

---

## Service management

When installed using `--install-service`, ServerDeck is managed by systemd.

### Check status

```bash
systemctl status serverdeck.service --no-pager
```

### Start

```bash
sudo systemctl start serverdeck.service
```

### Stop

```bash
sudo systemctl stop serverdeck.service
```

### Restart

```bash
sudo systemctl restart serverdeck.service
```

### Disable at boot

```bash
sudo systemctl disable serverdeck.service
```

---

## Updating ServerDeck

To replace an existing installation with a newer ServerDeck script:

```bash
sudo cp serverdeck.py /opt/serverdeck/serverdeck.py
sudo chmod 755 /opt/serverdeck/serverdeck.py
sudo systemctl restart serverdeck.service
```

Persistent ServerDeck data stored under:

```text
/var/lib/serverdeck/
```

is preserved when replacing the application script.

---

## Uninstalling the service

ServerDeck includes an uninstall option:

```bash
sudo ./serverdeck.py --uninstall-service
```

This removes the ServerDeck systemd service, installed application script and ServerDeck-managed backup schedule units.

Backup data and legacy ServerDeck job data are intentionally left in place.

The CopyParty service is managed separately from the CopyParty page and is not removed simply because the ServerDeck service is uninstalled.

---

## Important paths

| Purpose | Path |
|---|---|
| Installed ServerDeck script | `/opt/serverdeck/serverdeck.py` |
| Persistent ServerDeck data | `/var/lib/serverdeck/` |
| PAM configuration | `/etc/pam.d/serverdeck` |
| ServerDeck systemd service | `/etc/systemd/system/serverdeck.service` |
| CopyParty script | `/opt/copyparty/copyparty-sfx.py` |
| CopyParty systemd service | `/etc/systemd/system/copyparty.service` |
| Persistent filesystem mounts | `/etc/fstab` |
| ServerDeck Netplan overrides | `/etc/netplan/99-serverdeck-*.yaml` |

---

## Requirements

ServerDeck is intended for:

- Debian
- Ubuntu
- Raspberry Pi OS / compatible Debian-derived systems
- Python 3
- systemd-based installations

It relies on standard Linux system utilities where applicable, including:

- `apt-get`
- `systemctl`
- `hostnamectl`
- `mount`
- `umount`
- `lsblk`
- `findmnt`
- `blkid`
- PAM
- NetworkManager / `nmcli`
- Netplan
- `journalctl`

`rsync` is only required for rSync jobs and can be installed from the ServerDeck interface.

CopyParty is optional and can be downloaded directly from the CopyParty page.

Thumbnail support for CopyParty is also optional.

---

## Security

ServerDeck performs privileged system-management operations and should be treated as an administrative interface.

The built-in web server uses HTTP by default and does not provide TLS termination.

When entering Linux account credentials, only access ServerDeck:

- On a trusted private network
- Through a trusted VPN such as WireGuard or Tailscale
- Behind an HTTPS reverse proxy

Do **not** expose ServerDeck directly to the public internet without additional security controls.

Additional safeguards include:

- PAM-based Linux account authentication
- `sudo`-group restriction by default
- CSRF protection for modifying actions
- Confirmation prompts for destructive operations
- Network static-IP rollback protection
- UUID-based persistent disk mounts
- Protection against modifying the detected system disk
- CopyParty service ownership checks
- CopyParty service execution as the configuring Linux user rather than root

Remember that the integrated Terminal provides command access equivalent to the signed-in Linux user's shell.

---

## Project goals

ServerDeck aims to provide:

- A straightforward web interface for common server-management tasks
- A low-dependency, single-file deployment
- Familiar Linux account authentication
- A clean interface suitable for users who are less comfortable with the command line
- Useful safeguards around potentially disruptive operations
- A focused alternative to larger server-management platforms

The project intentionally concentrates on a smaller set of understandable and useful features instead of exposing every possible Linux system setting.

---

## Development status

ServerDeck is currently in the `0.9.x` testing phase.

The current development build documented by this README is:

```text
ServerDeck 0.9.7
```

The next planned milestone is:

```text
ServerDeck 1.0.0
```

after full testing on a clean Ubuntu Server installation, including validation of disk mounting and network configuration.

---

## Disclaimer

ServerDeck can perform administrative operations including:

- Installing and removing software packages
- Executing commands
- Creating scheduled backup jobs
- Mounting and unmounting filesystems
- Modifying `/etc/fstab`
- Changing network configuration
- Renaming the server
- Creating and deleting systemd services
- Restarting the system
- Shutting down the system

Incorrect disk, network, shell or package-management operations can cause data loss or make a server unreachable.

Maintain independent backups of important data and ensure local recovery access is available when testing system-level changes.

---

## License

Add your chosen open-source licence here.
