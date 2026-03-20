#!/usr/bin/env python3
"""
Minimal TCP listener to test if the scanner is sending ANY data.
No MURFI protocol parsing — just raw bytes.

Usage: python test_vsend_receive.py [port]
Default port: 15000
"""
import socket
import sys
import time

port = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
host = "0.0.0.0"

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((host, port))
sock.listen(5)
sock.settimeout(120)

my_ip = socket.gethostbyname(socket.gethostname())
print(f"=== Vsend TCP Test ===")
print(f"Listening on {host}:{port}")
print(f"This machine: {my_ip}")
print(f"Waiting up to 120 seconds for a connection...")
print(f">>> Start a scan on the scanner NOW <<<")
print()

try:
    conn, addr = sock.accept()
    print(f"[OK] CONNECTION from {addr[0]}:{addr[1]}")
    total = 0
    start = time.time()
    while True:
        try:
            conn.settimeout(10)
            data = conn.recv(65536)
            if not data:
                print(f"[OK] Connection closed by sender after {total} bytes")
                break
            total += len(data)
            elapsed = time.time() - start
            # Print first 32 bytes of first chunk as hex
            if total == len(data):
                preview = data[:32].hex(" ")
                print(f"[OK] First bytes: {preview}")
                # Check for ERTI magic (Vsend header)
                if data[:4] in (b"ERTI", b"SIMU"):
                    print(f"[OK] Vsend magic detected: {data[:4].decode()}")
                else:
                    print(f"[??] Unknown magic: {data[:4]}")
            print(f"  Received: {total:,} bytes total ({elapsed:.1f}s)", end="\r")
        except socket.timeout:
            print(f"\n[OK] No more data after {total:,} bytes total")
            break
    conn.close()
    if total > 0:
        print(f"\n=== SUCCESS: Scanner sent {total:,} bytes ===")
    else:
        print(f"\n=== CONNECTED but 0 bytes received ===")
except socket.timeout:
    print(f"[FAIL] No connection received in 120 seconds.")
    print()
    print("Possible causes:")
    print(f"  1. Scanner Vsend destination IP is not {my_ip}")
    print(f"  2. Scanner Vsend destination port is not {port}")
    print(f"  3. Scanner Vsend/Online Export is not enabled")
    print(f"  4. No scan sequence is running")
    print(f"  5. Firewall blocking incoming TCP on port {port}")
except KeyboardInterrupt:
    print("\nStopped by user.")
finally:
    sock.close()
