# Firewall Blocks vSend: A Debugging Account

**Date:** 2026-03-20
**System:** Ubuntu (nftables backend), Docker installed, Siemens scanner with vSend

## Symptom

Scanner console error during scan:

```
SendExternalFunctor::sendData() -- Cannot connect to 192.168.2.5:50000, deactivating vSend functionality
```

MURFI was running. The scanner could ping 192.168.2.5. Port and IP config were correct. No data was ever received across multiple sessions spanning two days.

## What we ruled out

| Check | Result |
|-------|--------|
| XML config (`sub-phantom/xml/2vol.xml`) | Port 50000, `receiveImages=true` — correct |
| MURFI binding | `ss -tlnp` showed `0.0.0.0:50000` — listening on all interfaces |
| Local TCP test | `nc -w1 192.168.2.5 50000` succeeded from the host itself |
| Network route | `ip route get 192.168.2.1` → via `enp3s0f1`, source `192.168.2.5` — correct |
| Ethernet interface | `192.168.2.5/24` on `enp3s0f1` — correct |
| Apptainer networking | Default host namespace, no `--net` flag — container shares host network |
| `ufw status` | Reported **inactive** |
| Scanner-side config | vSend destination IP and port verified on Siemens console |
| Scanner-side ping | `192.168.2.5` reachable from Siemens console |

Everything looked correct on both sides. `ufw status` said the firewall was off. MURFI was listening. The scanner could ping. Yet TCP connections to port 50000 failed silently.

## Root cause

### The iptables/nftables split

This machine had Docker installed. Docker adds its own iptables chains at startup. As a side effect, Docker loaded the `nf_tables` kernel module and activated the full iptables-via-nftables translation layer. This created a situation where:

1. **`ufw status`** reported "inactive" — ufw's own daemon was not managing rules
2. **`iptables -S`** showed a port 50000 ACCEPT rule (line 120 in the dump):
   ```
   -A ufw-user-input -s 192.168.2.1/32 -p tcp -m tcp --dport 50000 -j ACCEPT
   ```
3. **`nft list ruleset`** showed the actual enforced rules — and the port 50000 rule was **missing** from the `ufw-user-input` chain:
   ```
   chain ufw-user-input {
       meta l4proto udp udp dport { 137,138 } ... accept   # Samba
       meta l4proto tcp tcp dport { 139,445 } ... accept    # Samba
       meta l4proto tcp tcp dport 22 ... accept              # SSH
       meta l4proto tcp ip saddr 192.168.2.1 tcp dport 15001 ... accept  # infoserver
       # ← no port 50000 rule
   }
   ```

The INPUT chain policy was **DROP**. The kernel enforces nftables, not legacy iptables. So the iptables rule was a ghost — it existed in the legacy view but was never evaluated.

### Why pings worked but TCP didn't

The `ufw-before-input` chain (which runs before user rules) has:

```
meta l4proto icmp icmp type echo-request ... accept
```

ICMP echo (ping) is accepted early in the chain, before the default DROP policy applies. TCP to port 50000 had no matching ACCEPT rule in the nftables ruleset, so it fell through to DROP. The packets were silently discarded — no RST, no ICMP unreachable, just silence. From the scanner's perspective, the connection attempt timed out.

### A second issue hiding behind the first

The iptables rule that did exist was also too narrow:

```
-s 192.168.2.1/32 -p tcp --dport 50000 -j ACCEPT
```

This only allows the scanner **console** (192.168.2.1). But `SendExternalFunctor` runs on the **MARS** — the Siemens image reconstruction computer, which is a separate machine with its own IP (typically 192.168.2.3 or similar on the scanner's internal network). Even if the nftables rule had been present, it would have blocked the MARS.

## Fix

```bash
# Allow the entire scanner subnet to reach port 50000
sudo ufw allow from 192.168.2.0/24 to any port 50000 proto tcp
```

## Prevention

Updated `run_session.sh` and `feedback.sh` to check nftables directly during the pre-flight network check, rather than relying on `ufw status`. The check:

1. Reads the live `ufw-user-input` chain from nftables (`nft list chain`)
2. Verifies a port 50000 ACCEPT rule exists
3. Warns if the source is restricted to a single IP instead of the subnet

## Lessons

1. **`ufw status` can lie.** When Docker (or anything else) loads the nf_tables module and installs chains, the kernel enforces nftables rules. The iptables compatibility layer may show rules that are not in the nftables ruleset. Always check `nft list ruleset` for ground truth.

2. **Ping is not a connectivity test for TCP.** ICMP and TCP take different paths through the firewall. A successful ping proves L3 reachability, not that a specific TCP port is open. The right test is `nc -w1 <ip> <port>` from the actual source machine.

3. **The scanner console and the MARS are different machines.** Pinging from the Siemens console tests the console's network path, not the MARS reconstruction computer's. Firewall rules scoped to the console IP (192.168.2.1) will block the MARS. Allow the subnet.

4. **Silent DROP is the worst failure mode.** The firewall dropped packets without sending RST or ICMP unreachable. The scanner saw a timeout, not a rejection. This makes the problem look like "the computer isn't listening" rather than "the firewall is blocking." A REJECT rule would have been easier to diagnose.
