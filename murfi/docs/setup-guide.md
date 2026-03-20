# Mindfulness NF: Complete Setup Guide

Step-by-step instructions for configuring the MURFI workstation and Siemens scanner
for the mindfulness neurofeedback protocol. Written from the 2026-03-20 setup at
Pitt's BRIDGE Center (Siemens Prisma VE11C).

## 1. Workstation Hardware and OS

| Component | Spec |
|---|---|
| OS | Ubuntu 22.04.4 LTS |
| Kernel | 6.8.0-101-generic |
| CPU | 32 cores |
| RAM | 64 GB |
| GPU | NVIDIA (for MURFI Qt GUI, via Apptainer --nv) |
| Scanner interface | `enp3s0f1` (ethernet, 192.168.2.5/24) |

## 2. Network Configuration

### Workstation ethernet

The scanner interface is configured as a static connection named `murfi`:

```
Interface:  enp3s0f1
IP:         192.168.2.5/24
Gateway:    none (direct connection to scanner network)
```

Configured via NetworkManager:

```bash
# View current config
nmcli device show enp3s0f1

# If you need to recreate (as root):
nmcli connection add type ethernet con-name murfi ifname enp3s0f1 \
    ipv4.method manual ipv4.addresses 192.168.2.5/24
```

Wi-Fi must be OFF during scanning to avoid routing conflicts.

### Scanner network

```
Scanner console:     192.168.2.1
MARS (reconstruction): 192.168.2.x (varies, separate from console)
MURFI workstation:   192.168.2.5
```

The console and workstation are connected via a router on the 192.168.2.0/24 subnet.

### Firewall

Ubuntu uses nftables as the kernel packet filter. Docker installs iptables chains
that load nftables rules, creating a situation where `ufw status` reports "inactive"
but the kernel is actively filtering with a default INPUT DROP policy.

Required firewall rules:

```bash
# vSend (real-time volumes from scanner)
sudo ufw allow from 192.168.2.0/24 to any port 50000 proto tcp

# DICOM receiver (resting state bulk export)
sudo ufw allow from 192.168.2.0/24 to any port 4006 proto tcp

# Verify rules are in nftables (the actual enforcer):
sudo nft list chain ip filter ufw-user-input
# Should show lines with "dport 50000" and "dport 4006"
```

**Critical:** Use `192.168.2.0/24` (whole subnet), not `192.168.2.1` (console only).
The MARS reconstruction computer has a different IP than the console.
See `firewall-debugging-2026-03-20.md` for the full debugging account.

## 3. Software Installation

### MURFI (Apptainer container)

MURFI v2.1.1 runs in an Apptainer container at `/opt/murfi/`.

```
/opt/murfi/
├── apptainer-images/
│   └── murfi.sif              # Main container (built from ghcr.io/gablab/murfi:v2.1.1)
├── bin/
│   ├── murfi_apptainer        # Wrapper script (handles X11 forwarding)
│   └── murfi_wrapper          # Alternative wrapper (Docker-based, not used)
├── docker-images/
│   └── murfi.Dockerfile       # Dockerfile for building the image
└── subjects/                  # System-level subjects (not used, project has its own)
```

To build/update the container:

```bash
# Pull the Docker image and convert to Apptainer SIF
# (requires Docker to be installed for the initial pull)
docker pull ghcr.io/gablab/murfi:v2.1.1
apptainer build /opt/murfi/apptainer-images/murfi.sif docker-daemon://ghcr.io/gablab/murfi:v2.1.1
```

The container needs X11 libraries for MURFI's Qt GUI. The Dockerfile installs
`libxcb`, `libx11-xcb-dev`, `libglu1-mesa-dev`, `libxrender-dev`, `libxi-dev`,
`libxkbcommon-dev`, `libxkbcommon-x11-dev`.

### Group membership

The `young-lab` user must be in the `murfi` and `fsl` groups:

```bash
sudo usermod -aG murfi,fsl young-lab
# Log out and back in for group changes to take effect
```

### Apptainer

```bash
# Check version
apptainer --version   # 1.3.4

# Install if needed (Ubuntu 22.04):
sudo add-apt-repository -y ppa:apptainer/ppa
sudo apt update
sudo apt install -y apptainer
```

### FSL

FSL 6.0.7 is installed at `/opt/fsl` and loaded via `/etc/profile.d/fsl.sh`
for users in the `fsl` group.

```bash
# Verify FSL is available
flirt -version
```

### Python dependencies

The project uses `uv` for Python dependency management:

```bash
cd /home/young-lab/code/mindfulness-nf
uv sync   # Creates .venv/ and installs all dependencies
```

Key Python packages: `pynetdicom` (DICOM receiver), `pydicom` (DICOM parsing),
`pandas`, `numpy` (ICA component selection).

## 4. Scanner Configuration (Siemens VE11C)

### 4a. vSend (C2P agreement required)

vSend pushes reconstructed volumes to the workstation in real-time via TCP.
Used for: 2vol, feedback, and transfer runs (all have Motion correction ON).

The C2P agreement allows access to the Siemens ICE functor `SendExternalFunctor`.
Configuration is done in the Siemens service software or via the sequence protocol.

vSend destination: `192.168.2.5:50000`

The scanner sends both raw and MoCo volumes. MURFI's `onlyReadMoCo=true` filters
to keep only MoCo volumes.

### 4b. DICOM export

Standard DICOM C-STORE for bulk export after scan completion. Used for resting
state runs (Motion correction OFF, no vSend).

#### Add the DICOM node on the scanner

Open **Siemens Med Service Software** (Internet Explorer on scanner console):

1. Go to **DICOM > Network Nodes**
2. Fill in:
   - **edit Name:** `MURFI_DICOM`
   - **Host:** `MURFI` (must be in the scanner's hosts/routing table, mapped to 192.168.2.5)
   - **edit AE Title:** `MURFI` → click **Add**
   - **Port Number:** `4006`
   - **Connection mode:** select **Unsecure**
   - Check **Storage** under Supported DICOM services
3. Click **Verification** to test (requires the DICOM receiver to be running)
4. Click **Save**

#### Add the hostname mapping

If the Verification fails with "AE title did not respond", the hostname `MURFI`
needs to be resolved to `192.168.2.5`. On the Siemens service software:

1. Go to **TCP/IP LAN** or **Routing**
2. Add the hostname mapping: `MURFI` → `192.168.2.5`
3. Save and restart the application if prompted

Note: "restart application pending" appears at the bottom of the screen after
changes. Save and click Finish to apply.

#### AutoTransfers (optional)

Under **Service > AutoTransfers**, you can set up automatic DICOM export rules.
This requires a service software license. If you see "the selected function is
not available for your Service Software License", use manual transfer instead.

#### Manual transfer (Patient Browser)

After each resting state scan:

1. Open **Patient Browser** on the scanner console
2. Select the resting state series
3. Right-click → **Send** (or Transfer menu)
4. Choose **MURFI_DICOM** as destination
5. DICOMs are sent to the workstation

### 4c. Scanner sequences (from rt-BPD protocol PDFs)

The protocol uses these sequences under `\\USER\Auerbach\REMIND`:

| Protocol | Sequence | TR | Meas | MoCo | Data path | vSend needed |
|---|---|---|---|---|---|---|
| LOC3 / RT15 | `func-bold_task-2vol_run-01` | 1200ms | 2 | ON | vSend | Yes |
| LOC3 | `func-bold_task-rest_run-01` | 1200ms | 250 | OFF | DICOM | No |
| LOC3 | `func-bold_task-rest_run-02` | 1200ms | 250 | OFF | DICOM | No |
| RT15 | `func-bold_task-restpre_run-01` | 1200ms | 250 | OFF | DICOM | No |
| RT15 | `func-bold_task-restpre_run-02` | 1200ms | 250 | OFF | DICOM | No |
| RT15 | `func-bold_task-transferpre_run-01` | 1200ms | 150 | ON | vSend | Yes |
| RT15 | `func-bold_task-feedback_run-01..10` | 1200ms | 150 | ON | vSend | Yes |
| RT15 | `func-bold_task-transferpost_run-01` | 1200ms | 150 | ON | vSend | Yes |

All EPI sequences: 2mm iso, 72 slices, 128x128, GRAPPA 2.

vSend must be enabled on the scanner for sequences with MoCo ON. Resting state
sequences do not need vSend — they use DICOM export after scan completion.

## 5. DICOM Receiver

The Python DICOM receiver (`murfi/scripts/dicom_receiver.py`) accepts C-STORE
requests from the scanner and writes DICOM files to `murfi/dicom_input/`.

It is started/stopped automatically by `run_session.sh` around the resting state
steps. For manual testing:

```bash
cd /home/young-lab/code/mindfulness-nf
.venv/bin/python murfi/scripts/dicom_receiver.py --port 4006
```

Configuration:
- **AE Title:** `MURFI` (must match scanner Network Nodes config)
- **Port:** `4006` (unprivileged, no root needed)
- **Output:** `murfi/dicom_input/` (MURFI polls this directory when `imageSource=DICOM`)

## 6. Running the Pipeline

### Session 1: Localizer

```bash
cd /home/young-lab/code/mindfulness-nf/murfi/scripts
bash run_session.sh sub-001 localizer
```

Steps (automated by the script):
1. System setup (network check, firewall check)
2. **2vol scan** — vSend, MoCo ON. Start MURFI, then start scan on console.
3. **Resting state run 1** — DICOM receiver starts, MURFI reads from `dicom_input/`.
   Run the scan, then send from Patient Browser to MURFI_DICOM.
4. **Resting state run 2** — same as above.

### Between sessions: Processing

```bash
bash run_session.sh sub-001 process
```

Steps (~25 minutes):
1. Extract resting state networks (MELODIC ICA)
2. Select DMN/CEN components (rsn_get.py, bilateral CEN analysis)
3. Register masks to study_ref space (FLIRT)

### Session 2: Neurofeedback

```bash
bash run_session.sh sub-001 nf
```

Steps (12 runs total):
1. Transfer pre (no feedback) — vSend
2. Feedback runs 1-5 — vSend + PsychoPy
3. Transfer post (no feedback) — vSend
4. Feedback runs 6-10 — vSend + PsychoPy

## 7. MURFI XML Configuration

Each subject has three XML configs in `subjects/sub-XXX/xml/`:

| File | Input | Port | MoCo | Measurements |
|---|---|---|---|---|
| `2vol.xml` | vSend | 50000 | `onlyReadMoCo=true` | 20 |
| `rest.xml` | DICOM | — | — | 250 |
| `rtdmn.xml` | vSend | 50000 | `onlyReadMoCo=true` | 150 |

Templates are in `subjects/template/xml/xml_vsend/`. New subjects are created
from these templates by `createxml.sh`:

```bash
cd murfi/scripts
source createxml.sh sub-002 setup
```

## 8. Troubleshooting

### "SendExternalFunctor::sendData() — Cannot connect to 192.168.2.5:50000"

Scanner can't reach MURFI. Check in order:
1. Is MURFI running? (`ss -tlnp | grep 50000`)
2. Firewall? (`sudo nft list chain ip filter ufw-user-input | grep 50000`)
3. Is the firewall rule for the whole subnet, not just 192.168.2.1?
4. Is Wi-Fi off? (`ip addr show | grep 'wl[a-z]'`)

### DICOM Verification fails on scanner

1. Is the receiver running? (`ss -tlnp | grep 4006`)
2. Firewall rule for port 4006? (`sudo nft list chain ip filter ufw-user-input | grep 4006`)
3. Hostname `MURFI` resolves to 192.168.2.5 on the scanner?

### MURFI starts but receives 0 volumes

- Check `onlyReadMoCo` setting matches the sequence. MoCo ON sequences need `true`,
  resting state (MoCo OFF) needs DICOM input instead.
- Check the MURFI log: `subjects/sub-XXX/log/log.rtl`
- "ignoring non-MoCo image" without "got MoCo image" = scanner MoCo is OFF but MURFI
  expects it ON. Either enable MoCo on the sequence or set `onlyReadMoCo=false`.

### ufw says "inactive" but ports are blocked

Docker installs iptables chains that load nftables rules. The kernel enforces nftables
regardless of ufw daemon status. Always check:
```bash
sudo nft list chain ip filter ufw-user-input
```
See `firewall-debugging-2026-03-20.md` for the full analysis.

### MURFI output floods the terminal

MURFI's Qt GUI writes directly to `/dev/tty`, bypassing shell stdout redirects.
This is a known issue. The status line in `run_session.sh` shows the volume count
but MURFI output may interleave. Type `done` when the scan finishes — `read` captures
stdin regardless of terminal output.
