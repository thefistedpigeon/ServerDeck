ServerDeck

A lightweight, self-hosted web interface for managing Debian and Ubuntu servers.

ServerDeck is a single-file Python application designed to provide straightforward server management for users who may not be comfortable working entirely from the command line.

It was created as a smaller and more focused alternative to tools such as Cockpit, with an emphasis on simple installation, minimal dependencies and a clean web interface.

ServerDeck is suitable for Debian, Ubuntu and Raspberry Pi OS systems.

Features
Overview

The Overview page provides an at-a-glance view of the server, including:

System hostname
Server uptime
CPU usage
Memory usage
Disk storage usage
Network download speed
Network upload speed
Total network data received and transmitted
Operating-system information
System load averages
Restart-required status

The hostname can be changed directly from the interface by clicking it.

System updates

The Updates page allows users to:

Refresh the available package list
View available package updates
See installed and available package versions
Install updates with one click
View live update command output
Check whether a restart is required

ServerDeck uses the standard Debian and Ubuntu APT tools for package management.

rSync backups

The rSync page provides a graphical interface for creating and managing backup jobs.

Backup jobs can include:

Local or remote source and destination paths
Server-side folder browsing
Manual path entry
Immediate manual execution
Scheduled execution using systemd timers or cron
Persistent backup logs

Supported rsync options include:

--dry-run
--archive
--itemize-changes
--verbose
--human-readable
-P
--update
--chmod=

Jobs can be created, edited, enabled, disabled, run immediately or deleted from the web interface.

Web terminal

ServerDeck includes a simple browser-based terminal for running commands on the server.

The terminal:

Runs as the currently signed-in Linux user
Maintains a persistent shell session
Preserves working-directory changes such as cd
Displays command output in real time
Supports command history with the arrow keys
Allows running commands to be interrupted
Can restart or close the current shell session

The terminal is intended for normal command-line tasks. Full-screen interactive applications such as nano, vim, top and htop are better used through SSH.

Power controls

A persistent Power Options button is available from every page.

It provides confirmed actions for:

Restarting the server
Shutting down the server

Power requests are protected using authenticated sessions and CSRF validation.

Authentication

ServerDeck authenticates users against the server's local Linux accounts using PAM.

This means users can sign in with the same username and password they use for SSH.

By default, only members of the local sudo group may access ServerDeck. This prevents ordinary local accounts from gaining access to administrator-level server functions.

SSH keys cannot be entered directly into the browser. Accounts that normally use key-only SSH authentication must also have a Linux password configured to use the ServerDeck login page.

Installation

Download the latest ServerDeck Python script and make it executable:

chmod +x serverdeck.py

Install it as a systemd service:

sudo ./serverdeck.py --install-service

ServerDeck will normally be installed to:

/opt/serverdeck/serverdeck.py

The service can then be managed with:

sudo systemctl start serverdeck.service
sudo systemctl stop serverdeck.service
sudo systemctl restart serverdeck.service
sudo systemctl status serverdeck.service

Once running, open the interface in a browser:

http://SERVER-IP:9090/
Updating

To replace an existing installation with a newer script:

sudo cp serverdeck.py /opt/serverdeck/serverdeck.py
sudo chmod 755 /opt/serverdeck/serverdeck.py
sudo systemctl restart serverdeck.service

Existing backup jobs, logs and configuration stored under /var/lib/serverdeck are preserved.

Requirements

ServerDeck is intended for:

Debian
Ubuntu
Raspberry Pi OS
Python 3
systemd-based installations

It uses standard system utilities already commonly available on these platforms, including:

apt
systemctl
hostnamectl
rsync
PAM

No external Python web framework is required.

Security

ServerDeck performs privileged server-management operations and should be treated as an administrative interface.

The built-in web server uses HTTP by default. When entering Linux account credentials, use ServerDeck only:

On a trusted private network
Through a VPN such as WireGuard or Tailscale
Behind an HTTPS reverse proxy

Do not expose ServerDeck directly to the public internet without additional security controls.

Terminal sessions and authentication sessions are stored in memory and are cleared when the ServerDeck service restarts.

Project goals

ServerDeck aims to provide:

A simple interface for common server-management tasks
A low-dependency, single-file deployment
Familiar Linux account authentication
A clean interface suitable for less technical users
A focused alternative to larger server-management platforms

The project intentionally concentrates on a smaller set of reliable and understandable features rather than attempting to expose every possible system setting.

Disclaimer

ServerDeck can install software updates, execute commands, manage backups, rename the server, restart the system and shut it down.

Review commands, paths and backup settings carefully before running them. Always maintain separate backups of important data.

License

Add your chosen open-source licence here.
