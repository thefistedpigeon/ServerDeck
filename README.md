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

> **Current stable release:** v1.14.1  
> ServerDeck v1.14.1 keeps the combined CopyParty/Docker service-status card introduced in v1.14.0 while restoring the established ServerDeck v1.13 dark/light colour theme and visual treatment.

---

## Navigation

The current ServerDeck navigation is:

```text
Overview | Updates | Disks | Network | Users | Processes | Services | Backups | CopyParty | Docker | Terminal
```

The **Terminal** page intentionally remains the final page in the navigation.

A persistent **Uptime** indicator and **Power Options** button are available from every page. Uptime is placed between the theme selector and Power Options.

---










## v1.14.1 Classic Theme Restoration

ServerDeck v1.14.1 is a visual patch for v1.14.0. The combined **Services** card and its CopyParty/Docker status indicators remain unchanged, but the workspace returns to the established ServerDeck colour/theme treatment used before the v1.14 visual experiment.

- Restores the richer navy/blue dark workspace and blue/teal accents.
- Restores the original blue-grey Light theme.
- Restores gradient cards, original controls, borders, shadows, typography and spacing treatment from v1.13.
- Keeps the v1.14 combined Services card layout and lightweight one-shot status checks.
- Adds no background polling, dependencies or host-side work.

---

## v1.14.0 Overview Services & Sleek UI

ServerDeck v1.14.0 is a presentation and dashboard-polish release. It keeps the existing management workflows intact while making the interface feel flatter, calmer and more consistent.

### Overview services

The separate CopyParty shortcut is replaced by one compact **Services** card containing CopyParty and Docker. Each row is clickable and uses a simple status dot:

- **Green** — service is running.
- **Red** — service is installed/configured but stopped or failed.
- **Amber** — service is not configured/not installed, unavailable, or in an indeterminate warning state.

The card uses lightweight one-shot status endpoints when Overview opens. It does not poll Docker or CopyParty repeatedly and does not run container/image inventory commands merely to draw the dashboard.

### Visual refinement

The workspace adopts a flatter product-site-inspired style while retaining ServerDeck's own dark navy/blue identity:

- Solid surfaces replace most decorative gradients.
- More generous whitespace and a clearer type hierarchy.
- Softer borders and restrained shadows.
- Cleaner, rounded controls with simpler hover states.
- Less aggressive uppercase/letter-spacing in metric labels and table headers.
- Light mode uses a neutral off-white workspace while the header/navigation remain permanently dark.
- Existing responsive, keyboard and accessibility behaviours are retained.

No new background polling, host services, Python dependencies, or management daemons are introduced.

---

## v1.13.0 Guided Backups

ServerDeck v1.13.0 rebrands the former **rSync** page as **Backups** and turns backup creation into a guided workflow rather than a large configuration form. rsync remains the underlying transfer engine, but the normal UI is organised around the backup task rather than the command-line tool.

The setup sequence is:

```text
1. Select source folder
2. Select destination folder
3. Select backup options
4. Perform a mandatory dry run
5. Choose a schedule or on-demand only
6. Review and create the backup service
```

Key behaviour:

- A fresh installation with no saved backups opens directly in the setup wizard.
- Existing ServerDeck backup jobs are preserved and appear in Management mode with **Run now**, **Edit setup**, **View log**, and **Delete** controls.
- Editing an existing backup uses the same wizard and requires another successful dry run before changes can be saved.
- Source and local destination folders can be selected with ServerDeck's server-side folder browser. Remote rsync destinations such as `user@server:/path/` remain supported.
- Step 3 uses safe, understandable defaults for archive mode, partial transfers, itemised logging, human-readable output, and protection of newer destination files. `--chmod` and permanent dry-run mode remain available as advanced compatibility options.
- Step 4 runs the exact unsaved definition through `rsync --dry-run`; the destination is not changed. Changing source, destination, or transfer options invalidates the successful dry run and requires it to be repeated.
- ServerDeck rejects a local destination which is identical to, or nested inside, the local source folder to avoid recursive self-backups.
- Step 5 supports **On demand only** as a first-class choice, or scheduled operation through a systemd timer (recommended) or cron. Hourly, daily, weekly, monthly, and custom expressions remain supported.
- Every saved backup gets a predictable one-shot `serverdeck-backup-<id>.service` when systemd is active. Scheduled systemd backups additionally get a matching `.timer`.
- Deleting a backup removes its ServerDeck service/timer definition but never deletes files already copied to the destination.
- The page introduces no recurring polling loop; backup information is loaded on page entry and after explicit operations.

rsync can still be installed directly from the Backups page if it is missing.

---

## v1.12.0 Guided CopyParty Setup

ServerDeck v1.12.0 turns CopyParty installation into a five-step guided workflow while retaining the v1.10/v1.11 config manager and dedicated runtime underneath.

The setup sequence is:

```text
1. Download CopyParty
2. Configure copyparty:copyparty and base storage
3. Create CopyParty accounts/groups and shared folders
4. Review curated features
5. Preview, install, enable and start the service
```

Key behaviour:

- New/partial installs resume at the first incomplete step rather than showing every management control at once.
- A recommended managed storage root such as `/srv/copyparty` is owned by `copyparty:copyparty` with setgid permissions so new ServerDeck-created subfolders have predictable access.
- ServerDeck only auto-creates new shared folders underneath that base storage root. Existing arbitrary data folders remain usable, but ServerDeck does not recursively take ownership of them during setup.
- Existing ServerDeck-managed CopyParty services open directly in **Management mode**, preserving their configuration and runtime.
- Legacy ServerDeck services can still use the v1.11 dedicated-account migration workflow.
- An unrelated `/etc/systemd/system/copyparty.service` is detected as **External** and never overwritten automatically. Fresh Start can remove that unit only after explicit acknowledgement. Package/vendor systemd units loaded from `/usr/lib`, `/lib`, or another deployment location are never deleted by ServerDeck.
- **Fresh Start** stops/removes the eligible service, clears ServerDeck's CopyParty model/generated config and can clear CopyParty-owned `.config/.cache` runtime state. It never deletes shared files or the dedicated Linux service account.
- Setup Health in Management reports script, runtime account, storage, generated configuration, service unit and running status on demand when the page is opened.
- CopyParty still has no recurring ServerDeck polling loop.

CopyParty's upstream documentation recommends config-file management for accounts/volumes and supports configuration reloads for those changes; `[global]` changes still require a restart. The upstream systemd example also uses a dedicated `copyparty` account and `/var/lib/copyparty` working directory.

---

## v1.11.0 Dedicated CopyParty Runtime

ServerDeck v1.11.0 changes the default Linux runtime identity for new CopyParty services. Instead of running file sharing as whichever administrator configured it, ServerDeck creates and uses a dedicated `copyparty:copyparty` system account.

Highlights:

- New CopyParty service installations automatically create the `copyparty` system user/group when needed
- Dedicated home directory at `/var/lib/copyparty`
- Interactive login disabled with `nologin`/`false`
- Existing ServerDeck-managed CopyParty services remain unchanged until the administrator chooses **Migrate to dedicated account**
- Migration checks every configured shared folder before changing the service
- Failed migration attempts restore the previous systemd unit/configuration and previous runtime account
- Maintenance shows the current service identity, dedicated account state, filesystem-access checks and hardening state
- Read/write requirements are checked as the actual `copyparty` account using `runuser`
- When a volume's owning Linux group would safely resolve a permission issue, ServerDeck can add `copyparty` to that non-privileged supplementary group
- Privileged groups such as `root`, `sudo`, `docker`, `disk`, `shadow`, `adm`, `systemd-journal` and `lxd` are never offered automatically
- The service uses `/var/lib/copyparty` as its working directory and XDG config home
- Additional systemd hardening protects kernel/control-group/namespace interfaces and applies conservative memory/swap limits
- No new recurring polling or background work is introduced

ServerDeck never recursively changes ownership of shared folders during migration. Existing application/media ownership is preserved; Linux permissions or supplementary group membership are adjusted only when the administrator explicitly chooses to do so.

---

## v1.10.0 CopyParty Configuration Manager

ServerDeck v1.10.0 replaces the original single-folder anonymous read/write CopyParty setup with a configuration-driven management page. Existing ServerDeck CopyParty installations are migrated in-place the first time the new configuration is saved.

Highlights:

- Multiple shared folders / CopyParty volumes
- Friendly access presets: public read-only/read-write, upload-only, authenticated-only, selected accounts/groups, and custom permissions
- CopyParty-specific accounts and groups, separate from Linux users
- Password changes without exposing credentials through the API, command previews or activity history
- Curated CopyParty features: LAN discovery, indexing/search, media metadata, thumbnails and temporary share links
- Generated config preview with account passwords redacted
- Config-file service at `/etc/copyparty/serverdeck.conf`
- `systemctl reload copyparty` support for account/volume changes
- Automatic restart when `[global]` feature settings change
- Existing download/update, thumbnail dependency and service controls retained
- No background CopyParty polling; status is read when the page opens or is manually refreshed

---

## v1.9.0 Users & Groups

ServerDeck v1.9.0 adds a focused **Users & Groups** page for local Linux account administration. It reads the normal passwd/group databases directly and performs no recurring background polling.

### Users

The Users tab provides:

- Searchable normal-user list, with system/service accounts hidden by default
- Optional **Show system accounts** view
- Username, display name, UID/GID, home directory, login shell, groups and account state
- Create a local user with optional home directory, display name, initial password, shell and supplementary groups
- Change display name, login shell and supplementary group membership
- Change a normal user's password
- Lock or unlock a normal account
- Delete a normal account, with **Delete home directory** kept as a separate explicit option
- Root, system-account and current-session protections

Passwords are sent directly to Linux through `chpasswd` standard input. They are never placed in process arguments, ServerDeck activity records or persistent ServerDeck configuration.

### Groups

The Groups tab provides:

- Searchable local group inventory
- GID and member display
- Optional system-group visibility
- Create new local groups
- Edit supplementary membership of normal groups
- Delete eligible local groups

Critical groups, ServerDeck authentication groups, system groups and groups currently used as a user's primary group are protected from unsafe deletion. Administrative membership is normally best changed through the individual user's detail panel.

### SSH authorised keys

Normal users with a usable home directory have an **SSH authorised keys** section in their user detail panel. ServerDeck can add or remove OpenSSH public keys in:

```text
~/.ssh/authorized_keys
```

When it writes the file, ServerDeck enforces `0700` on `.ssh`, `0600` on `authorized_keys`, and restores ownership to the selected Linux account. ServerDeck never creates, accepts or stores SSH private keys.

### Safety

- `root` is read-only from the Users page.
- System/service accounts are read-only.
- The account currently signed in to ServerDeck cannot be locked or deleted.
- The signed-in account cannot be removed from its final ServerDeck-authorised administrator group.
- The signed-in account cannot be changed to a `nologin`/`false` shell.
- System and critical groups are protected from unsafe modification/deletion.
- Account actions are recorded in ServerDeck activity history without passwords or key contents.

## v1.8.1 Persistent dark application header

ServerDeck's branding/header and primary navigation now stay in the dark navy ServerDeck colour scheme in Dark, Light and System themes. Light mode changes only the workspace below the header: page backgrounds, cards, forms, tables and dialogs. This keeps the application chrome visually consistent and avoids the header losing contrast against the ServerDeck icon/branding.

- Brand/logo row remains dark.
- Full-width navigation remains dark.
- Theme, uptime, Power Options and account controls remain dark.
- Light/System-Light still applies to the management workspace.
- No backend or polling changes.

## v1.8.0 Docker, Portainer and full-width navigation

ServerDeck v1.8.0 keeps the application focused on server management while delegating specialist container administration to Portainer.

### Navigation redesign

The header is now split into two deliberate rows:

```text
[ServerDeck]                                      Theme | Uptime | Power | Account
Overview | Updates | Disks | Network | Processes | Services | rSync | CopyParty | Docker | Terminal
```

- Branding and administrative controls stay in the compact top row.
- Primary navigation receives its own full-width row on desktop displays.
- Core host-management pages appear first.
- **rSync**, **CopyParty** and **Docker** are grouped toward the end as application/tool pages.
- **Terminal remains the final navigation item.**
- Intermediate widths can scroll the navigation horizontally rather than wrapping it awkwardly.
- Small/mobile layouts use the existing compact Menu control.

### Docker Engine

The new **Docker** page can detect an existing Docker installation or install Docker Engine when it is absent.

Automatic installation:

- Uses Docker's official APT repository rather than the convenience script.
- Supports Ubuntu and Debian directly.
- Supports current Raspberry Pi OS through Docker's supported Debian ARM packages.
- Installs Docker Engine, the Docker CLI, containerd, Buildx and the Docker Compose plugin.
- Detects conflicting distro packages before installation and clearly lists them before they are removed.
- Enables and starts `docker.service` after installation.

When Docker is installed, ServerDeck shows:

- Running/stopped state
- Docker version
- Docker Compose plugin version
- Container count
- Image count
- systemd service state

ServerDeck provides Start, Stop and Restart controls for Docker Engine. Stopping Docker also stops its running containers, so the UI requires confirmation.

### Portainer CE

Once Docker is running, ServerDeck can deploy **Portainer Community Edition** using the official LTS image.

The ServerDeck deployment uses:

```text
Container:  portainer
Image:      portainer/portainer-ce:lts
HTTPS:      9443
Edge port:  8000
Data:       portainer_data
```

The persistent `portainer_data` Docker volume keeps Portainer configuration separate from the container itself.

The Docker page can:

- Install Portainer CE
- Open the Portainer HTTPS interface
- Start Portainer
- Stop Portainer
- Restart Portainer
- Delete a Portainer container created by ServerDeck

Deleting the ServerDeck-created Portainer container deliberately keeps the `portainer_data` volume so the configuration can be reused. ServerDeck will not delete an unrelated/existing Portainer container it did not create.

### Why Portainer instead of a built-in container manager?

ServerDeck remains the lightweight host-management interface. Portainer handles container-specific workflows such as containers, stacks, images, volumes and networks. This avoids duplicating a much larger container-management interface inside ServerDeck.

### Logs page removed

The general **Logs** page introduced in v1.5.0 has been removed from ServerDeck in v1.8.0. CopyParty can still display its own recent service output where it is relevant to CopyParty troubleshooting, and rSync continues to keep its own job logs.

### Resource behaviour

The Docker page performs a status read when it is opened or manually refreshed. It does not add a new global polling loop or restore the removed global health/update polling behaviour.

## v1.7.0 unified interface and design system

ServerDeck v1.7.0 is a presentation and interaction consistency release. It does not add another management subsystem; instead, it makes the existing pages behave and look like parts of the same application.

### Shared design system

- Added shared design tokens for spacing, corner radii, control height, type scale, surfaces, focus treatment and semantic status colours.
- Standardised page headers so every management page uses the same title, description and right-side status/action structure.
- Standardised card surfaces, section headings, internal spacing and hover/focus behaviour.
- Reduced decorative shadow intensity so information hierarchy comes primarily from spacing, borders and typography.
- Standardised form controls, labels, help text, checkbox cards and advanced-option panels.
- Standardised table headers, row spacing, hover feedback, list toolbars and pagination presentation.
- Standardised dialogs and detail panels with consistent sizing, header separation and spacing.
- Standardised empty/loading presentation with a common visual treatment instead of unrelated blank-table states.

### Actions and status

- Buttons continue to use a predictable semantic hierarchy:
  - **Primary** for apply/install/create/run actions.
  - **Secondary** for routine view/refresh/edit actions.
  - **Warning** for disruptive but reversible actions.
  - **Danger** for destructive or forceful actions.
  - **Ghost** for dismiss/secondary navigation actions.
- Common actions now receive consistent small icons while retaining their text labels, so the UI never relies on icon meaning alone.
- Status pills now share one dot-and-label treatment: teal for healthy/active states, amber for warnings/pending states, red for failures, and neutral grey for informational states.
- Existing status wording and backend behaviour are preserved.

### Navigation and responsive behaviour

- Simplified the active navigation treatment to a subtle ServerDeck-blue underline rather than a heavy filled tab.
- Normalised header control sizes and spacing around Theme, Uptime, Power Options and account controls.
- Improved list-heavy toolbars on narrower screens so search/filter controls wrap predictably.
- Improved card and modal spacing for phone/tablet widths while keeping the existing mobile table-card layouts.
- Added `prefers-reduced-motion` support for users who disable interface animation.

### Keyboard and accessibility refinement

- Active navigation exposes `aria-current="page"`.
- Busy buttons expose `aria-busy` as well as their disabled/loading state.
- Error notices use alert semantics; normal notices use status semantics.
- Modal workflows focus the first usable control when opened and return keyboard focus to the originating control when closed.
- Strong focus rings remain consistent across buttons, links, inputs, selects, textareas, summaries and other focusable controls.
- The existing `/` search shortcut and `Esc` dialog shortcut remain available.

### Low-resource behaviour preserved

The UI consistency layer is browser-side and does **not** introduce additional host polling or background management work.

- No global health strip or global health polling was reintroduced.
- No background APT availability loop was reintroduced.
- The Overview live system counters still run only while Overview is visible.
- Processes and Services retain their existing page-local visibility-aware behaviour. Docker status is loaded on demand and does not add a global poll.
- The small DOM consistency observer only responds to interface elements that ServerDeck has already rendered; it does not query the server.

Internally, authenticated pages now pass through a shared markup harmoniser before rendering. This provides common structural hooks for page headers, section headers and action groups so future pages can inherit ServerDeck's established design without duplicating bespoke markup rules.

---

## v1.6.2 header and Overview interaction refinement

ServerDeck v1.6.2 makes the remaining Overview shortcuts simpler and moves uptime somewhere more useful.

- **Uptime** now appears in the persistent header between the theme control and **Power Options**.
- The header uptime is read once when a page is rendered and then advances locally in the browser; it does not add a polling endpoint or recurring host query.
- Removed the separate Uptime card from Overview.
- Preserved system load averages by moving them into the CPU card detail line.
- The entire **Hostname** card is now clickable and opens the rename dialog.
- The Hostname card includes the helper text **Click to rename the machine**.
- The entire **CopyParty** card is now clickable and opens the CopyParty management page.
- The CopyParty card includes the helper text **Click to manage**.
- Removed the small action buttons/pills previously embedded inside those cards.

---

## v1.6.1 Overview cleanup

ServerDeck v1.6.1 continues the low-resource direction of v1.6.0 by removing summary information that duplicated dedicated management pages.

- Removed the Overview attention/status cards for **Updates**, **Services**, **Disks** and **CopyParty**.
- Removed the old `/api/dashboard` aggregation endpoint entirely.
- Overview no longer inventories all systemd services or disks merely to build decorative summary cards.
- Added a compact **CopyParty** status card on Overview. v1.6.2 makes the full card clickable and moves Uptime into the persistent header.
- The Overview CopyParty card uses a lightweight one-shot status endpoint and does not fetch CopyParty logs, package information or thumbnail status.
- Recent ServerDeck activity remains on Overview and refreshes only on page load or when **Refresh** is pressed.
- Live CPU, memory, root-storage and network figures continue to refresh only while the Overview page is visible.
- APT update availability remains entirely demand-driven from the Updates page.

---

## v1.6.0 low-overhead UI and branding

ServerDeck v1.6.0 focuses on keeping the management interface out of the server's way when it is not actively being used.

### New ServerDeck icon

- Added the new flat ServerDeck server-stack icon beside the ServerDeck name in the main header.
- The same icon is used on the login page and as the browser favicon.
- The standalone Python script embeds an optimised 64×64 copy of the icon, preserving ServerDeck's single-file deployment model.
- The release bundle also includes a 512×512 transparent PNG for repository/release artwork.

### Lower idle resource use

- Removed the global CPU/RAM/disk/update/restart health strip from every authenticated page.
- Removed the 15-second global `/api/health` browser poll and the `/api/health` endpoint.
- Removed background APT availability checks from Overview/dashboard refreshes.
- Update availability is now **demand-driven**: ServerDeck runs the simulated APT upgrade check when the Updates page is opened/refreshed or when an explicit package operation needs it.
- v1.6.0 stopped background APT checks and made update availability demand-driven.
- v1.6.1 subsequently removes the Overview attention cards altogether.
- The live CPU/RAM/network values on **Overview itself** still refresh while that page is visible because those values come from lightweight kernel/proc counters and are the purpose of the Overview page.

This prevents a ServerDeck tab left open on another page from repeatedly spawning `apt-get -s ... upgrade` merely to keep decorative global status information current.

---

## v1.5.0 logs, SMART health and UI refinement

ServerDeck v1.5.0 focuses on making the existing interface easier to use while adding two practical read-only diagnostic features.

### System Logs (removed in v1.8.0)

v1.5.0 introduced a **Logs** page that provided a bounded view of the systemd journal without requiring SSH.

It supported:

- All journal entries
- Current-boot logs
- Kernel logs
- Error-only logs
- Logs for a selected systemd service
- Time ranges from the last hour through all available entries
- Message/source/unit/PID search
- 100, 250, 500 or 1000 row limits
- Optional 3-second live refresh while the page is visible
- Entry detail dialogs
- Direct **View Logs** links from service details (removed with the Logs page in v1.8.0)

At the time, live mode used repeated bounded journal queries rather than leaving a persistent `journalctl -f` process running on the host. The general Logs page and its live refresh were removed in v1.8.0.

### SMART drive health

The **Disks** page now includes a read-only drive-health section for physical disks when `smartctl` is available.

It can display, where the drive exposes the information:

- Overall SMART health
- Temperature
- Power-on hours
- Reallocated sectors
- Pending sectors
- Offline-uncorrectable sectors
- NVMe percentage-used/wear information
- Drive model, serial number and protocol

SMART information is cached for 60 seconds to avoid unnecessary repeated queries. For SATA/SAS/USB disks ServerDeck asks `smartctl` not to wake a drive that is already in standby merely to refresh the UI.

If `smartmontools` is not installed, the Disks page offers an **Install SMART tools** button.

ServerDeck does not expose SMART self-tests, firmware operations or other write/destructive SMART commands in this release.

### Interface refinement

v1.5.0 also adds:

- **Dark**, **Light** and **System** colour themes
- Persistent browser theme preference
- Sticky headers for large process/service/log tables
- Mobile card layouts for those tables
- Pagination for Processes and Services
- Clickable sortable headers
- Remembered process/service sort and page-size choices
- Remembered Process/Service column visibility
- Service favourites/pinning
- Favourite-only service filter
- Direct service-to-journal navigation
- Stronger keyboard focus indicators
- `/` keyboard shortcut to focus the current page search box
- `Esc` to close the active modal/dialog
- Clearer detail-dialog context labels
- More consistent action symbols and destructive-action presentation
- Improved empty states, including an explicit **No Wi-Fi adapter detected** message

---

## v1.4.0 wireless networking

Version 1.4.0 extends the existing Network page when a wireless network device is detected.

### Wireless scanning and connection

- Wireless controls appear only when Linux exposes a Wi-Fi interface.
- Scan for nearby access points on demand; ServerDeck does not continuously scan in the background.
- Results show SSID, BSSID, signal strength, band, channel, security, connection state and whether a profile is already saved.
- Connect to open, WPA/WPA2 Personal and WPA3 Personal networks.
- Reconnect to saved NetworkManager profiles without re-entering a stored password.
- Configure hidden networks by entering the SSID manually.
- Disconnect the selected wireless adapter.
- Forget saved NetworkManager profiles or ServerDeck-managed Netplan Wi-Fi definitions.
- Enterprise/802.1X and WEP networks are identified but are deliberately not configured by ServerDeck.

### Backend support

With **NetworkManager**, ServerDeck uses `nmcli` for scanning, creating/activating connection profiles, disconnecting and forgetting networks. NetworkManager credentials are supplied through a temporary root-only credential file rather than being placed in command arguments.

With **Netplan/networkd**, ServerDeck writes a dedicated root-only Wi-Fi definition under `/etc/netplan/98-serverdeck-wifi-*.yaml` and applies it using Netplan. `wpa_supplicant` is required by the networkd Wi-Fi backend; scanning uses `iw` or `wpa_cli` when available.

Wi-Fi passwords are not written to the ServerDeck activity history. Netplan files containing credentials are written with mode `0600`.

---

## v1.3.0 quality-of-life improvements

Version 1.3.0 focuses on improving how the existing tools feel to use rather than adding another major management subsystem.

### Global interface improvements

- v1.3 introduced a compact global health strip; it was removed in v1.6.0 to minimise idle host resource use.
- Success and error actions now produce consistent toast notifications while page-local operation output is still retained.
- Button colours have clearer meaning: primary actions for create/apply/start, amber for disruptive-but-reversible actions, and red for forceful/destructive actions.
- Navigation remains horizontal on desktop and automatically changes to a compact menu on narrower screens.
- Advanced settings are collapsed by default on pages where the basic workflow does not require them.
- Selected filters, sort modes and several advanced-panel choices are remembered for the current browser session.

### Overview evolution

v1.3 introduced attention cards for updates, services, disks and CopyParty. These were useful while ServerDeck was expanding, but they duplicated dedicated pages and required extra host queries. **v1.6.1 removes those attention cards** in favour of a simpler Overview. A compact CopyParty status remains beside Hostname/Uptime, and recent ServerDeck activity remains beneath the live system metrics.

### Processes and Services details

Processes and Services use cleaner main tables. Clicking a row or **Details** opens a focused information panel containing the full metadata and relevant actions.

Process and service search/filter choices are retained during the browser session, and both pages show the number of currently visible rows.

### Command previews

ServerDeck now provides previews before several host-level configuration actions, including:

- Backup/rsync command construction
- Disk mount command/persistent-mount intent
- New systemd service unit content
- ServerDeck service reconfiguration

### Activity history

ServerDeck records administrative actions performed through the web interface to a small rotating JSONL activity log under the ServerDeck data directory.

Examples include service changes, process termination, disk mounts, network changes, package operations, CopyParty management, backup jobs and hostname/power actions.

**Terminal commands are deliberately not recorded.**

### Diagnostics and maintenance

Click the signed-in username in the header to open the ServerDeck System panel. It provides:

- ServerDeck version/build
- Python, OS, kernel and systemd versions
- Current port, authentication mode/groups and data directory
- Network configuration backend
- Copy-to-clipboard diagnostics
- Recent activity history
- Configuration export
- In-app port and PAM-group reconfiguration
- Secure-cookie setting
- Optional GitHub release checking and self-update

Configuration export creates a compressed archive containing ServerDeck configuration and ServerDeck-managed systemd/Netplan information while intentionally excluding passwords, sessions and terminal content.

### Optional release checking / self-update

ServerDeck does not guess the project's GitHub repository URL. To enable update checking, enter the repository's GitHub latest-release API URL in **System → Maintenance**:

```text
https://api.github.com/repos/OWNER/REPO/releases/latest
```

When configured, ServerDeck can check the latest release and, when a newer Python release asset is available, download it, validate it with Python compilation, preserve the current script as `serverdeck.py.previous`, replace the installed script and restart `serverdeck.service`.

---

## Features

### Overview

The Overview page provides a focused at-a-glance view of the server without duplicating the detailed status available on dedicated management pages.

It includes:

- System hostname with a full-card rename action
- Operating-system information
- System load averages (shown with CPU details)
- CPU usage
- Memory usage
- Root filesystem storage usage
- Current network download speed
- Current network upload speed
- Total network data received
- Total network data transmitted
- Active network interfaces included in traffic statistics
- Restart-required status
- Combined clickable **Services** card for CopyParty and Docker with green/amber/red status dots
- Recent ServerDeck administrative activity

The hostname can be changed by clicking anywhere on the Hostname card. CopyParty and Docker can be opened directly from the Services card. Server uptime is displayed persistently in the header between the theme control and Power Options, without recurring server polling.

Network activity is calculated from Linux network counters and excludes the loopback interface.

---

### Updates

The Updates page provides a graphical interface for Debian and Ubuntu APT package management.

To keep ServerDeck lightweight, update availability is checked only when this page is opened/refreshed or when an explicit update workflow requires it. ServerDeck does not run recurring background APT simulations from other pages.

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

### Backups

The Backups page provides a guided interface for creating, validating, running, and scheduling file backups using rsync.

The six-step wizard covers:

1. Source folder
2. Destination folder
3. Transfer options
4. Mandatory safe dry run
5. Schedule or on-demand operation
6. Backup name, review, and service creation

It supports:

- Local source paths
- Local destination paths
- Remote rsync destinations
- Server-side folder browsing
- Mandatory pre-save dry-run validation
- On-demand execution
- Scheduled backups using systemd timers or cron
- Persistent backup logs
- Editing existing backups through the same guided workflow
- Deleting backup definitions without deleting destination data
- Installing `rsync` from the web interface when needed

Recommended rsync behaviour is exposed with plain-language controls while advanced compatibility options remain available. Underneath, ServerDeck can use:

- `--dry-run`
- `--archive`
- `--itemize-changes`
- `--verbose`
- `--human-readable`
- `-P`
- `--update`
- `--chmod=`

When systemd is active, every saved backup receives a one-shot service named:

```text
serverdeck-backup-<id>.service
```

Scheduled systemd backups additionally receive a matching timer. On-demand backups can be run at any time from ServerDeck.

ServerDeck stores backup definitions and logs under:

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
- View read-only SMART health for physical disks
- Install `smartmontools` from the interface if SMART tools are missing

#### SMART health

When `smartctl` is installed, ServerDeck displays drive-health cards for physical disks. SMART queries are read-only and cached for 60 seconds. Depending on the drive type and firmware, ServerDeck can display temperature, power-on hours, sector-health counters and NVMe wear information.

For non-NVMe drives, ServerDeck uses smartctl's standby-aware query mode so a sleeping drive is not deliberately spun up just to refresh the page.

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

The Network page displays current interface information, provides IPv4 configuration and, when a wireless adapter is detected, adds Wi-Fi scanning and connection management.

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

#### Wireless networking

If a wireless interface is detected, a **Wireless networks** section appears on the Network page. It can:

- Select between detected Wi-Fi adapters
- Scan for nearby access points on demand
- Show SSID, BSSID, signal strength, frequency band, channel and security
- Mark the currently connected network
- Mark saved networks
- Connect to open networks
- Connect to WPA/WPA2 Personal networks
- Connect to WPA3 Personal networks
- Reconnect saved profiles
- Configure hidden SSIDs
- Disconnect a wireless adapter
- Forget a saved network

ServerDeck does not run periodic background Wi-Fi scans; scanning occurs when **Scan for networks** is selected.

WPA Enterprise/802.1X and legacy WEP networks are displayed as unsupported rather than attempting an incomplete configuration.

On systems using NetworkManager, ServerDeck manages Wi-Fi profiles with `nmcli`. Passwords are supplied through a temporary `0600` credential file and are not included in command arguments or activity-history entries.

On systems using Netplan/networkd, ServerDeck creates a dedicated Wi-Fi file such as:

```text
/etc/netplan/98-serverdeck-wifi-wlan0.yaml
```

The file is written mode `0600`. The networkd Wi-Fi backend requires `wpa_supplicant`; scanning uses `iw` or `wpa_cli` when available.

Changing the Wi-Fi network that is carrying the current ServerDeck/SSH connection can immediately disconnect the browser and may assign the server a different IP address. Local console access or a second network interface is recommended when changing a remote server's wireless connection.

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

ServerDeck includes optional integration with [CopyParty](https://github.com/9001/copyparty), a standalone file-sharing server. v1.12.0 separates **installation/setup** from **day-to-day management**.

#### Guided setup

A new or reset installation follows five steps:

1. **Download** — fetch/update the official CopyParty SFX script at `/opt/copyparty/copyparty-sfx.py`.
2. **Service account & storage** — create/normalise the dedicated `copyparty:copyparty` no-login account and select a managed base storage folder (recommended `/srv/copyparty`).
3. **Accounts & folders** — create CopyParty-only accounts/groups and at least one shared folder/volume. New paths beneath the configured base folder can be created automatically with predictable `copyparty` ownership.
4. **Features** — review LAN discovery, share links, thumbnails, indexing/search and metadata. Indexing remains off by default.
5. **Review & install** — preview the redacted config, then install/enable/start the hardened systemd service.

Once the ServerDeck-managed service exists, the page switches to **Management mode** with Shared folders, Accounts & access, Features and Maintenance tabs.

#### Managed base storage

ServerDeck recommends a dedicated root such as:

```text
/srv/copyparty/
├── family/
├── media/
├── uploads/
└── private/
```

The base directory is owned by `copyparty:copyparty`, uses setgid directory permissions, and is verified for read/write/traverse access. ServerDeck-created subfolders beneath the base are also assigned to the dedicated runtime.

For safety, ServerDeck does **not** recursively re-own an existing non-empty data collection during guided setup. Existing folders outside the managed base can still be added as volumes and use the existing v1.11 filesystem/group-access checks.

#### Shared folders / volumes

Each volume can use friendly access presets:

- Public read-only
- Public read/write
- Public upload-only
- Any signed-in CopyParty account: read-only or read/write
- Selected CopyParty accounts/groups: read-only or read/write
- Custom read/write/move/delete/admin/dotfile permissions

Removing a volume never deletes its files.

#### Accounts and groups

CopyParty accounts remain deliberately separate from Linux/ServerDeck accounts. ServerDeck can create/delete accounts, change passwords, and create groups for easier volume permission assignment. Passwords are not returned by the API, included in activity records, or shown in config previews.

#### Features

ServerDeck exposes a curated subset of CopyParty settings:

- LAN discovery (`z`)
- File indexing/search (`e2dsa`)
- Media metadata indexing (`e2ts`)
- Thumbnail master/video/audio controls
- Temporary authenticated share links (`shr`)

Indexing is disabled by default. The UI warns that large initial scans can consume CPU/disk I/O and may temporarily block up2k uploads while CopyParty reloads/scans filesystem state.

Optional thumbnail dependencies can be installed with the equivalent of:

```bash
sudo apt install --no-install-recommends python3-pil ffmpeg
```

#### Configuration and service

ServerDeck generates:

```text
/etc/copyparty/serverdeck.conf
/etc/systemd/system/copyparty.service
```

and runs:

```bash
/opt/copyparty/copyparty-sfx.py -c /etc/copyparty/serverdeck.conf
```

New services run as `copyparty:copyparty` with `/var/lib/copyparty` as the runtime home/working directory and interactive login disabled. Account/volume changes use CopyParty's `USR1` reload path; global feature changes restart the service.

#### Existing installations and Fresh Start

ServerDeck classifies CopyParty setup state rather than blindly overwriting it:

- **No/partial setup:** resume the guided wizard at the first incomplete step.
- **ServerDeck-managed service:** open Management mode directly.
- **Older ServerDeck service:** preserve it and offer the dedicated-account migration when applicable.
- **External `/etc/systemd/system/copyparty.service`:** block overwrite and offer an explicit Fresh Start takeover.
- **Package/vendor systemd unit elsewhere:** detect it but refuse to delete it; remove it through its original package/deployment method first.

Fresh Start returns ServerDeck to the guided setup by removing eligible service/configuration state. It can optionally remove the downloaded script and reset CopyParty-owned `.config/.cache` runtime state. It always preserves shared user files and retains the dedicated Linux service account.

#### Setup Health

Management mode provides an on-demand health summary for:

- CopyParty script
- Dedicated runtime account
- Base storage
- Generated config
- systemd service
- Running state

This is calculated when the page is opened or refreshed; it does not add background polling.


---


### Users & Groups

The **Users & Groups** page manages local Linux accounts and groups without adding a Python dependency. Human accounts are shown by default; system accounts are optional and read-only.

Supported user operations include:

- Create normal users
- Change display name, shell and supplementary groups
- Change passwords without storing them
- Lock/unlock normal accounts
- Delete accounts, optionally including their home directory
- Add/remove SSH public keys

Supported group operations include creating local groups, editing supplementary membership and deleting eligible non-system groups. Root, system accounts, critical groups and the currently signed-in account receive additional safeguards.

The page loads account data on demand and does not run a background refresh loop.

### Processes

The Processes page provides a live view of processes currently present on the Linux host.

It displays:

- PID and parent PID
- Linux user
- Current CPU usage
- Resident memory usage and percentage
- Process state
- Process age
- Full command line where available
- Search and sortable columns
- 50/100/200-row pagination
- Remembered User/State column visibility
- Mobile-friendly card presentation on narrow screens

The process list refreshes approximately every **2.5 seconds** while the page is visible. Automatic polling pauses when the browser tab is hidden to avoid unnecessary host activity.

Processes can be controlled directly from the page:

- **Stop** sends `SIGTERM`, allowing the process an opportunity to exit cleanly.
- **Kill** sends `SIGKILL`, immediately terminating the process when it cannot be stopped normally.

Both actions require confirmation. PID 1 and the ServerDeck process itself are protected from termination through the web interface.

The process inventory is read directly from Linux `/proc`, so no additional Python package such as `psutil` is required.

---

### Services

The Services page provides a systemd service manager directly inside ServerDeck.

It lists installed and currently loaded `.service` units and shows:

- Service name and description
- Running, stopped or failed state
- Detailed active/sub-state
- Enabled, disabled, static, masked or transient state
- Unit-file location
- Main service activation method
- Timer, socket or path triggers where systemd exposes them
- Timer schedule and next run time where available
- Boot-enabled services
- Services that are normally activated manually or as dependencies
- Favourite/pinned services
- Search, filtering and sortable columns
- 50/100/200-row pagination
- Remembered Trigger/Description column visibility

The list refreshes approximately every **5 seconds** while the page is visible. Automatic polling pauses when the browser tab is hidden.

Available controls include:

- **Start**
- **Stop**
- **Restart**
- **Enable** at boot
- **Disable** at boot

ServerDeck protects `serverdeck.service` from stop, restart, disable and delete actions on this page.

#### Creating a service

The **Add service** form can create a new systemd service with:

- Service name
- Description
- Command / `ExecStart`
- Linux user account
- Optional working directory
- Restart policy: `on-failure`, `always` or `no`
- Enable-at-boot option
- Start-immediately option

New services are written to:

```text
/etc/systemd/system/<name>.service
```

and marked as ServerDeck-created units. Generated units are validated with `systemd-analyze verify` when that tool is available before they are enabled or started.

#### Deleting a service

Delete is intentionally limited to **regular local service files under `/etc/systemd/system`**.

ServerDeck will not delete package/vendor service definitions under locations such as:

```text
/usr/lib/systemd/system
/lib/systemd/system
```

Deleting a local service stops and disables it, removes the local `.service` file and reloads systemd. It does **not** uninstall the application, executable or data used by that service.

Services managed by another ServerDeck feature, such as CopyParty and ServerDeck-generated backup units, should be deleted from their owning page instead.

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
- Backup jobs

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
| Activity history | `/var/lib/serverdeck/activity.jsonl` |
| ServerDeck maintenance settings | `/var/lib/serverdeck/settings.json` |
| ServerDeck Wi-Fi profile metadata | `/var/lib/serverdeck/wifi-profiles.json` |
| PAM configuration | `/etc/pam.d/serverdeck` |
| ServerDeck systemd service | `/etc/systemd/system/serverdeck.service` |
| Local services created/managed from Services | `/etc/systemd/system/*.service` |
| CopyParty script | `/opt/copyparty/copyparty-sfx.py` |
| CopyParty systemd service | `/etc/systemd/system/copyparty.service` |
| CopyParty runtime home | `/var/lib/copyparty` |
| User SSH public keys | `/home/<user>/.ssh/authorized_keys` (per account) |
| Docker repository | `/etc/apt/sources.list.d/docker.sources` |
| Docker signing key | `/etc/apt/keyrings/docker.asc` |
| Docker data (Docker-managed) | `/var/lib/docker/` |
| Persistent filesystem mounts | `/etc/fstab` |
| ServerDeck IPv4 Netplan overrides | `/etc/netplan/99-serverdeck-*.yaml` |
| ServerDeck Wi-Fi Netplan definitions | `/etc/netplan/98-serverdeck-wifi-*.yaml` |

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
- `smartctl` from `smartmontools` for optional SMART drive-health information
- `iw` or `wpa_cli` for Wi-Fi scanning on Netplan/networkd systems
- `wpa_supplicant` for Wi-Fi when Netplan uses the networkd renderer

`rsync` is only required for Backups and can be installed from the ServerDeck interface.

CopyParty is optional and can be downloaded directly from the CopyParty page.

Thumbnail support for CopyParty is also optional.

`smartmontools` is optional and can be installed from the Disks page when SMART health is wanted.

`iw` and `wpa_supplicant` are only required for relevant wireless functionality on systems where NetworkManager is not handling Wi-Fi.

---



Local user/group management uses standard Debian/Ubuntu account tools supplied by the base system, including `useradd`, `usermod`, `userdel`, `groupadd`, `groupdel`, `gpasswd`, `passwd`, and `chpasswd`. ServerDeck reads account/group data through Python's standard-library `pwd` and `grp` modules.

Docker and Portainer are optional. If Docker is not installed, the Docker page can install the official Docker CE packages on supported Ubuntu, Debian and Raspberry Pi OS hosts. Portainer is then deployed as a Docker container and is not a Python dependency of ServerDeck.

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
- Wi-Fi passwords are excluded from ServerDeck activity history and temporary NetworkManager credential files are mode `0600`
- ServerDeck Netplan Wi-Fi files containing credentials are mode `0600`
- UUID-based persistent disk mounts
- Protection against modifying the detected system disk
- CopyParty service ownership checks
- CopyParty service execution as a dedicated no-login `copyparty` system account on new installations
- Safe CopyParty runtime migration with per-volume Linux permission checks and rollback on failed service startup
- CopyParty supplementary-group assistance is limited to non-privileged groups that directly resolve a configured volume permission issue
- Detailed confirmations for disruptive/destructive actions
- Activity history for web-interface administrative actions
- Configuration exports exclude stored passwords and live session/terminal data
- Root and system accounts are read-only on the Users page
- The signed-in account cannot be locked, deleted, stripped of its final ServerDeck-authorised admin group, or given a non-login shell
- User passwords are passed to `chpasswd` over stdin and never recorded in ServerDeck activity history
- SSH private keys are never accepted or stored; public-key files are written with restricted ownership/permissions
- Package/vendor systemd service files are protected from deletion on the Services page
- ServerDeck prevents its own systemd service from being stopped or disabled through the Services page
- SMART functionality in v1.5.0 is read-only; self-tests and device-setting changes are not exposed

Remember that the integrated Users, Services, Processes and Terminal pages provide powerful host-management capabilities. Account administration, service control, process termination and shell access should be limited to trusted administrators.

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

## Release status

ServerDeck v1.12.0 is the current stable release, introducing the guided CopyParty setup/fresh-start workflow while preserving the dedicated v1.11 runtime, config-driven v1.10 management model, and ServerDeck's low-resource behaviour.

```text
ServerDeck 1.12.0
```

---

## Disclaimer

ServerDeck can perform administrative operations including:

- Installing and removing software packages
- Executing commands
- Creating scheduled backup jobs
- Mounting and unmounting filesystems
- Modifying `/etc/fstab`
- Changing network configuration
- Creating, modifying, locking and deleting local Linux user accounts and groups
- Editing users' SSH `authorized_keys` files
- Scanning, connecting, disconnecting and forgetting Wi-Fi networks
- Reading system journal entries and SMART disk-health information
- Renaming the server
- Creating and deleting systemd services
- Creating, starting, stopping, enabling, disabling and deleting local systemd services
- Sending termination signals to running processes
- Restarting the system
- Shutting down the system

Incorrect disk, network, shell or package-management operations can cause data loss or make a server unreachable.

Maintain independent backups of important data and ensure local recovery access is available when testing system-level changes.

---

## License

GNU GPL
