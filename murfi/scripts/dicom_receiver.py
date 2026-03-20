#!/usr/bin/env python3
"""
DICOM receiver for resting state scans.

Listens for DICOM C-STORE from the Siemens scanner and writes files to a
directory that MURFI polls (imageSource=DICOM).

Usage:
    python dicom_receiver.py [--port 4006] [--output /path/to/dir] [--ae-title MURFI]

Scanner config (Siemens Med Service Software > DICOM > Network Nodes):
    AE Title:  MURFI
    IP:        192.168.2.5
    Port:      4006
"""
import argparse
import os
import sys
import signal
from pathlib import Path

from pynetdicom import AE, evt, StoragePresentationContexts, VerificationPresentationContexts


def handle_store(event, output_dir):
    """Handle incoming C-STORE request — save DICOM to output directory."""
    ds = event.dataset
    ds.file_meta = event.file_meta

    # Name file by SOP Instance UID to avoid collisions
    filename = f"{ds.SOPInstanceUID}.dcm"
    filepath = Path(output_dir) / filename
    ds.save_as(filepath)

    # Log progress — series/instance number for monitoring
    series = getattr(ds, "SeriesNumber", "?")
    instance = getattr(ds, "InstanceNumber", "?")
    desc = getattr(ds, "SeriesDescription", "")
    print(f"  Received: series {series} / instance {instance}  {desc}  → {filename}")

    return 0x0000  # Success


def main():
    parser = argparse.ArgumentParser(description="DICOM receiver for resting state scans")
    parser.add_argument("--port", type=int, default=4006, help="Port to listen on (default: 4006)")
    parser.add_argument("--output", type=str,
                        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                             "dicom_input"),
                        help="Directory to write DICOM files (default: ../dicom_input)")
    parser.add_argument("--ae-title", type=str, default="MURFI", help="AE Title (default: MURFI)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    ae = AE(ae_title=args.ae_title)

    # Accept verification (C-ECHO) and all standard storage SOP classes
    ae.supported_contexts = VerificationPresentationContexts + StoragePresentationContexts

    handlers = [(evt.EVT_C_STORE, handle_store, [str(output_dir)])]

    print(f"=== DICOM Receiver ===")
    print(f"AE Title:  {args.ae_title}")
    print(f"Port:      {args.port}")
    print(f"Output:    {output_dir}")
    print(f"Waiting for DICOM data from scanner...")
    print()

    # Clean shutdown on Ctrl+C or SIGTERM
    def shutdown(sig, frame):
        print("\nShutting down DICOM receiver...")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Block until killed
    ae.start_server(("0.0.0.0", args.port), evt_handlers=handlers, block=True)


if __name__ == "__main__":
    main()
