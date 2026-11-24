#!/usr/bin/env python3
"""Backup the encrypted EPUB vault from production to local timestamped versions.

Usage examples:
    python backup_prod_vault.py
    python backup_prod_vault.py --keep 30
    python backup_prod_vault.py --list
    python backup_prod_vault.py --ssh-key C:\\Users\\yabo\\Downloads\\jason1.pem
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = os.environ.get("TOOLBOX_PROD_HOST", "byang.japaneast.cloudapp.azure.com")
DEFAULT_USER = os.environ.get("TOOLBOX_PROD_USER", "jason")
DEFAULT_REMOTE_DIR = os.environ.get("TOOLBOX_REMOTE_VAULT_DIR", "/opt/toolbox/data/epub_vault")
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("TOOLBOX_VAULT_BACKUP_DIR", str(PROJECT_ROOT / "backups" / "epub_vault"))
)
VERSION_DIR_RE = re.compile(r"^\d{8}_\d{6}(?:_\d{2})?$")


def discover_ssh_key(explicit_key: Optional[str] = None) -> Optional[Path]:
    """Locate the SSH private key for the production machine."""
    if explicit_key:
        p = Path(explicit_key).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Specified SSH key does not exist: {p}")
        return p

    env_key = os.environ.get("TOOLBOX_SSH_KEY")
    if env_key:
        p = Path(env_key).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"TOOLBOX_SSH_KEY does not exist: {p}")
        return p

    candidates: List[Path] = []
    repo_ssh = PROJECT_ROOT / ".ssh"
    if repo_ssh.is_dir():
        for child in sorted(repo_ssh.iterdir()):
            if child.is_file() and not child.name.endswith((".pub", "known_hosts", "config")):
                candidates.append(child)

    home = Path.home()
    candidates.extend(
        [
            home / "Downloads" / "jason1.pem",
            home / ".ssh" / "jason1.pem",
            home / ".ssh" / "id_ed25519",
            home / ".ssh" / "id_rsa",
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _safe_rel_member_path(member_name: str) -> Optional[str]:
    norm = posixpath.normpath(member_name.replace("\\", "/")).lstrip("./")
    if not norm or norm == ".":
        return None
    parts = [p for p in norm.split("/") if p]
    if not parts or any(p in ("..", ".") for p in parts):
        raise ValueError(f"Unsafe path in vault archive: {member_name!r}")
    return "/".join(parts)


def fetch_remote_vault_tar_gz(
    *,
    host: str,
    user: str,
    remote_dir: str,
    ssh_key: Optional[Path],
    port: int = 22,
    timeout_sec: int = 180,
) -> bytes:
    """Stream a tar.gz snapshot of the remote vault directory over SSH."""
    quoted_dir = "'" + remote_dir.replace("'", "'\"'\"'") + "'"
    remote_cmd = (
        f"sudo test -f {quoted_dir}/vault.meta && "
        f"sudo test -f {quoted_dir}/catalog.enc && "
        f"sudo tar -C {quoted_dir} --exclude='*.tmp-*' --exclude='dpapi.bin' -czf - ."
    )

    ssh_cmd = [
        "ssh",
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if ssh_key is not None:
        ssh_cmd.extend(["-i", str(ssh_key)])
    ssh_cmd.extend([f"{user}@{host}", remote_cmd])

    proc = subprocess.run(
        ssh_cmd,
        capture_output=True,
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Remote vault stream failed (exit {proc.returncode}): "
            f"{stderr_msg or 'vault not initialized or SSH connection failed'}"
        )
    if not proc.stdout:
        raise RuntimeError("Received empty archive stream from remote server")
    return proc.stdout


def _allocate_version_id(output_dir: Path, now_local: Optional[datetime.datetime] = None) -> str:
    ts = (now_local or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    if not (output_dir / ts).exists():
        return ts
    for idx in range(1, 100):
        candidate = f"{ts}_{idx:02d}"
        if not (output_dir / candidate).exists():
            return candidate
    raise RuntimeError(f"Too many backups created within timestamp {ts}")


def store_versioned_backup_from_tar_bytes(
    tar_gz_bytes: bytes,
    *,
    output_dir: Path,
    host: str,
    remote_dir: str,
    now_local: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Validate tar.gz vault bytes and persist a local timestamped backup version."""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    local_dt = now_local or datetime.datetime.now().astimezone()
    utc_dt = datetime.datetime.now(datetime.timezone.utc)
    version_id = _allocate_version_id(output_dir, local_dt)

    staging_dir = output_dir / f".tmp_{version_id}_{os.getpid()}"
    final_dir = output_dir / version_id
    extracted_vault_dir = staging_dir / "vault"

    try:
        extracted_vault_dir.mkdir(parents=True, exist_ok=True)
        files_manifest: List[Dict[str, Any]] = []
        seen_paths = set()

        with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tf:
            for member in tf.getmembers():
                rel_path = _safe_rel_member_path(member.name)
                if not rel_path:
                    continue
                if member.isdir():
                    (extracted_vault_dir / rel_path).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue

                extracted_stream = tf.extractfile(member)
                if extracted_stream is None:
                    continue
                data = extracted_stream.read()

                dest_file = (extracted_vault_dir / rel_path).resolve()
                if not str(dest_file).startswith(str(extracted_vault_dir.resolve())):
                    raise ValueError(f"Path traversal blocked for {rel_path}")

                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.write_bytes(data)

                seen_paths.add(rel_path)
                files_manifest.append(
                    {
                        "path": rel_path,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )

        if "vault.meta" not in seen_paths or "catalog.enc" not in seen_paths:
            raise ValueError(
                "Invalid vault backup: archive is missing required 'vault.meta' or 'catalog.enc'"
            )

        files_manifest.sort(key=lambda item: item["path"])
        archive_filename = f"epub_vault_{version_id}.tar.gz"
        archive_path = staging_dir / archive_filename
        archive_path.write_bytes(tar_gz_bytes)

        object_files = [
            f for f in files_manifest if f["path"].startswith("objects/") and f["path"].endswith(".bin")
        ]
        total_uncompressed_bytes = sum(f["size"] for f in files_manifest)

        manifest: Dict[str, Any] = {
            "version": version_id,
            "timestampLocal": local_dt.isoformat(),
            "timestampUtc": utc_dt.isoformat().replace("+00:00", "Z"),
            "sourceHost": host,
            "remoteVaultDir": remote_dir,
            "archiveFile": archive_filename,
            "archiveByteLength": len(tar_gz_bytes),
            "archiveSha256": hashlib.sha256(tar_gz_bytes).hexdigest(),
            "fileCount": len(files_manifest),
            "objectCount": len(object_files),
            "totalVaultBytes": total_uncompressed_bytes,
            "files": files_manifest,
        }

        manifest_json = json.dumps(manifest, indent=2)
        (staging_dir / "backup_manifest.json").write_text(manifest_json, encoding="utf-8")

        os.replace(staging_dir, final_dir)
        (output_dir / "latest_manifest.json").write_text(manifest_json, encoding="utf-8")
        manifest["backupDir"] = str(final_dir)
        return manifest
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def list_local_backups(output_dir: Path) -> List[Dict[str, Any]]:
    """Return metadata for all timestamped backup versions sorted oldest -> newest."""
    if not output_dir.is_dir():
        return []
    versions: List[Dict[str, Any]] = []
    for child in sorted(output_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not VERSION_DIR_RE.match(child.name):
            continue
        manifest_path = child / "backup_manifest.json"
        if manifest_path.is_file():
            try:
                meta = json.loads(manifest_path.read_text(encoding="utf-8"))
                meta["backupDir"] = str(child.resolve())
                versions.append(meta)
                continue
            except Exception:
                pass
        versions.append(
            {
                "version": child.name,
                "timestampLocal": "unknown",
                "objectCount": -1,
                "totalVaultBytes": 0,
                "archiveSha256": "",
                "backupDir": str(child.resolve()),
            }
        )
    return versions


def prune_old_backups(output_dir: Path, keep: int) -> List[str]:
    """Keep only the newest `keep` backup versions and delete older ones."""
    if keep <= 0:
        return []
    versions = list_local_backups(output_dir)
    if len(versions) <= keep:
        return []
    to_remove = versions[: len(versions) - keep]
    removed_ids: List[str] = []
    for item in to_remove:
        shutil.rmtree(item["backupDir"], ignore_errors=True)
        removed_ids.append(item["version"])
    return removed_ids


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KiB"
    return f"{num_bytes / (1024 * 1024):.2f} MiB"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backup the encrypted EPUB vault from the production server to local timestamped versions."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Production SSH host (default: {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER, help=f"Production SSH username (default: {DEFAULT_USER})")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument(
        "--ssh-key",
        default=None,
        help="Path to SSH private key (auto-detects .ssh/* or ~/Downloads/jason1.pem if omitted)",
    )
    parser.add_argument(
        "--remote-dir",
        default=DEFAULT_REMOTE_DIR,
        help=f"Remote vault directory (default: {DEFAULT_REMOTE_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Local directory for timestamped backups (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=0,
        help="Optional number of newest backup versions to retain (0 = keep all)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List existing local timestamped backups without connecting to the remote server",
    )

    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir).expanduser().resolve()

    if args.list:
        backups = list_local_backups(output_dir)
        if not backups:
            print(f"No local vault backups found under {output_dir}")
            return 0
        print(f"Local vault backups in {output_dir} ({len(backups)} version(s)):")
        for b in backups:
            sha_short = (b.get("archiveSha256") or "")[:12]
            print(
                f"  - {b['version']}  |  objects={b.get('objectCount', '?')}  |  "
                f"size={_format_bytes(int(b.get('totalVaultBytes', 0)))}  |  "
                f"sha256={sha_short}  |  {b['backupDir']}"
            )
        return 0

    ssh_key = discover_ssh_key(args.ssh_key)
    key_desc = str(ssh_key) if ssh_key else "default ssh-agent / ~/.ssh config"
    print(f"[1/3] Connecting to {args.user}@{args.host}:{args.remote_dir} (key: {key_desc})...")

    tar_gz_bytes = fetch_remote_vault_tar_gz(
        host=args.host,
        user=args.user,
        remote_dir=args.remote_dir,
        ssh_key=ssh_key,
        port=args.port,
    )

    print(f"[2/3] Verifying and saving timestamped backup ({_format_bytes(len(tar_gz_bytes))} compressed)...")
    manifest = store_versioned_backup_from_tar_bytes(
        tar_gz_bytes,
        output_dir=output_dir,
        host=args.host,
        remote_dir=args.remote_dir,
    )

    print(
        f"[3/3] Backup complete!\n"
        f"      Version     : {manifest['version']}\n"
        f"      Location    : {manifest['backupDir']}\n"
        f"      Vault Files : {manifest['fileCount']} ({manifest['objectCount']} encrypted .bin objects)\n"
        f"      Total Size  : {_format_bytes(manifest['totalVaultBytes'])}\n"
        f"      SHA-256     : {manifest['archiveSha256']}"
    )

    if args.keep > 0:
        removed = prune_old_backups(output_dir, args.keep)
        if removed:
            print(f"      Pruned {len(removed)} older backup version(s): {', '.join(removed)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
