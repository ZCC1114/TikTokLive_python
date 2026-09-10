#!/usr/bin/env python3
"""Configure the dedicated TikTok LIVE domain on its Ubuntu server.

Run as root after public DNS and inbound TCP 80/443 are configured. No DNS
records, firewall rules, application tokens, or other virtual hosts are changed.
"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_IPV4 = "163.7.2.127"
MARKER = "Managed by TikTokLive deploy/configure_nginx.py"
CONFIG = Path("/etc/nginx/conf.d/tiktok-live.conf")
WEBROOT = Path("/var/www/tiktoklive-acme")
HOOK = Path("/etc/letsencrypt/renewal-hooks/deploy/tiktok-live-nginx")
HOOK_RECEIPT = Path("/run/tiktoklive-nginx-renewal-hook.json")
BACKUPS = Path("/var/backups/tiktoklive-nginx")


def domain_name(value: str) -> str:
    domain = value.lower().removesuffix(".")
    labels = domain.split(".")
    label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
    if len(domain) > 253 or len(labels) < 2 or any(not label.fullmatch(part) for part in labels):
        raise argparse.ArgumentTypeError("Use a complete ASCII DNS name, for example live.example.com")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise argparse.ArgumentTypeError("Use a DNS name, not an IP address")


def email_address(value: str) -> str:
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[^@\s]+", value):
        raise argparse.ArgumentTypeError("Invalid account email address")
    local, host = value.rsplit("@", 1)
    return f"{local}@{domain_name(host)}"


def health_locations() -> str:
    return """    location = /healthz {
        proxy_pass http://127.0.0.1:8765;
        proxy_connect_timeout 2s;
        proxy_read_timeout 3s;
        access_log off;
    }
    location = /readyz {
        allow 127.0.0.1;
        allow ::1;
        deny all;
        proxy_pass http://127.0.0.1:8765;
        proxy_connect_timeout 2s;
        proxy_read_timeout 3s;
        access_log off;
    }
"""


def render_config(domain: str, *, tls: bool) -> str:
    domain = domain_name(domain)
    fallback = f"return 308 https://{domain}$request_uri;" if tls else "return 503;"
    config = f"""# {MARKER}
# This file is included inside nginx's http context on Ubuntu.
map $http_upgrade $tiktok_live_upgrade {{
    default upgrade;
    '' close;
}}
limit_req_zone $binary_remote_addr zone=tiktok_live_handshake:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=tiktok_live_peer:10m;
limit_conn_zone $server_name zone=tiktok_live_total:1m;

server {{
    listen 80;
    server_name {domain};
    server_tokens off;
    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}
{health_locations()}
    location / {{ {fallback} }}
}}
"""
    if not tls:
        return config
    return config + f"""
server {{
    listen 443 ssl;
    server_name {domain};
    server_tokens off;
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:tiktok_live_tls:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    client_max_body_size 16k;
    client_header_timeout 10s;
    send_timeout 10s;
    access_log /var/log/nginx/tiktok-live.access.log;
    error_log /var/log/nginx/tiktok-live.error.log warn;
{health_locations()}
    location ^~ /ws/ {{
        limit_req zone=tiktok_live_handshake burst=30 nodelay;
        limit_req_status 429;
        limit_conn tiktok_live_peer 100;
        limit_conn tiktok_live_total 500;
        limit_conn_status 429;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $tiktok_live_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 3s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 10s;
        proxy_buffering off;
        proxy_request_buffering off;
        # No URI suffix: preserve /ws/<live_id> and the original query string.
        proxy_pass http://127.0.0.1:8765;
    }}
    location / {{ return 404; }}
}}
"""


def render_hook(domain: str, nginx: str, systemctl: str) -> str:
    # repr creates Python literals; the generated hook also never invokes a shell.
    return f"""#!/usr/bin/python3
# {MARKER}
import json
import os
import subprocess
import time
from pathlib import Path

if os.environ.get("RENEWED_LINEAGE") == {str(Path('/etc/letsencrypt/live') / domain)!r}:
    subprocess.run([{nginx!r}, "-t"], check=True)
    subprocess.run([{systemctl!r}, "reload", "nginx"], check=True)
    Path({str(HOOK_RECEIPT)!r}).write_text(json.dumps({{
        "lineage": os.environ["RENEWED_LINEAGE"], "completed_at_ns": time.time_ns(),
    }}) + "\\n")
"""


def run(arguments: list[str], *, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, check=True, text=True, timeout=timeout)


def verify_dns(domain: str) -> None:
    addresses = {item[4][0] for item in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    # This server currently has only a configured IPv4 public endpoint. Extra A
    # or AAAA destinations can make ACME and client connections intermittent.
    if addresses != {EXPECTED_IPV4}:
        raise RuntimeError(f"DNS must resolve only to {EXPECTED_IPV4}; observed: {sorted(addresses)}")
    print(f"DNS precheck passed: {domain} -> {EXPECTED_IPV4}", flush=True)


def verify_https(domain: str) -> None:
    """Verify the real certificate/hostname and upstream health over local TLS."""
    context = ssl.create_default_context()
    with socket.create_connection(("127.0.0.1", 443), timeout=5) as transport:
        with context.wrap_socket(transport, server_hostname=domain) as connection:
            connection.sendall(f"GET /healthz HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            if response.status != 200 or json.loads(response.read(4096)) != {"status": "ok"}:
                raise RuntimeError("HTTPS certificate is valid but upstream /healthz is not healthy")
    print(f"Local HTTPS certificate, hostname and application health verified for {domain}", flush=True)


def await_verification(probe: Callable[[], None], *, timeout: float = 10.0) -> None:
    """nginx reload signals its master; new workers can become ready later."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            probe()
            return
        except (OSError, RuntimeError, http.client.HTTPException, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def verify_http_challenge(domain: str) -> None:
    """Check webroot routing/permissions before asking the CA to validate."""
    challenge_directory = WEBROOT / ".well-known/acme-challenge"
    with tempfile.NamedTemporaryFile(dir=challenge_directory, prefix="preflight-", delete=False) as handle:
        token = Path(handle.name)
        expected = b"tiktok-live-acme-preflight"
        handle.write(expected)
        os.fchmod(handle.fileno(), 0o644)

    def probe() -> None:
        connection = http.client.HTTPConnection("127.0.0.1", 80, timeout=3)
        try:
            connection.request("GET", f"/.well-known/acme-challenge/{token.name}", headers={"Host": domain})
            response = connection.getresponse()
            if response.status != 200 or response.read(4096) != expected:
                raise RuntimeError("Local HTTP-01 webroot check failed")
        finally:
            connection.close()

    try:
        await_verification(probe)
    finally:
        token.unlink(missing_ok=True)
    print(f"Local HTTP-01 webroot verified for {domain}", flush=True)


def verify_renewal_hook(domain: str, *, since_ns: int) -> None:
    # Certbot can return zero when a deploy hook fails. Check evidence written
    # only after this hook has tested and reloaded nginx during the dry run.
    try:
        receipt = json.loads(HOOK_RECEIPT.read_text())
        if (
            receipt.get("lineage") != str(Path("/etc/letsencrypt/live") / domain)
            or not isinstance(receipt.get("completed_at_ns"), int)
            or receipt["completed_at_ns"] < since_ns
        ):
            raise ValueError("Receipt is stale or belongs to another certificate")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("Certbot dry run did not confirm successful nginx deploy-hook execution") from exc


@dataclass
class Snapshot:
    content: bytes | None
    mode: int = 0o644


def snapshot(path: Path) -> Snapshot:
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace a symlink: {path}")
    if not path.exists():
        return Snapshot(None)
    if not path.is_file():
        raise RuntimeError(f"Not a regular file: {path}")
    content = path.read_bytes()
    if MARKER.encode() not in content:
        raise RuntimeError(f"Refusing to overwrite an unmanaged file: {path}")
    return Snapshot(content, stat.S_IMODE(path.stat().st_mode))


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore(path: Path, previous: Snapshot) -> None:
    if previous.content is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, previous.content, previous.mode)


def install_config(
    content: str, nginx: str, systemctl: str, *, verify: Callable[[], None] | None = None,
) -> None:
    previous = snapshot(CONFIG)
    atomic_write(CONFIG, content.encode(), 0o644)
    try:
        # Treat conflicting virtual-host warnings as failure too: nginx -t can
        # otherwise return success while silently ignoring a duplicate name.
        checked = subprocess.run([nginx, "-t"], check=True, text=True, capture_output=True, timeout=30)
        if "conflicting server name" in checked.stderr.lower():
            raise RuntimeError("nginx reported a conflicting server name")
        print(checked.stderr.strip(), flush=True)
        active = subprocess.run([systemctl, "is-active", "--quiet", "nginx"], timeout=30).returncode == 0
        run([systemctl, "reload" if active else "start", "nginx"], timeout=30)
        if verify is not None:
            verify()
    except BaseException:
        restore(CONFIG, previous)
        # The last good configuration is restored even if testing/reloading it
        # also fails because of an unrelated site or service problem.
        try:
            run([nginx, "-t"], timeout=30)
            if subprocess.run([systemctl, "is-active", "--quiet", "nginx"], timeout=30).returncode == 0:
                run([systemctl, "reload", "nginx"], timeout=30)
        except (subprocess.SubprocessError, OSError):
            print(f"Previous {CONFIG} restored; nginx still needs operator inspection", flush=True)
        raise


def configure(domain: str, email: str | None) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Run this script as root on the target server")
    binaries = {name: shutil.which(name) for name in ("nginx", "certbot", "systemctl")}
    if any(value is None for value in binaries.values()):
        raise RuntimeError("Install the Ubuntu nginx and certbot packages first")
    nginx, certbot, systemctl = (binaries[name] for name in ("nginx", "certbot", "systemctl"))
    main_config = Path("/etc/nginx/nginx.conf").read_text()
    if not re.search(r"\binclude\s+/etc/nginx/conf\.d/\*\.conf\s*;", main_config):
        raise RuntimeError("nginx.conf must include /etc/nginx/conf.d/*.conf inside its http block")
    run([nginx, "-t"], timeout=30)
    verify_dns(domain)

    Path("/run/lock").mkdir(parents=True, exist_ok=True)
    with Path("/run/lock/tiktoklive-nginx.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prior_config, prior_hook = snapshot(CONFIG), snapshot(HOOK)
        BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-")
        backup = Path(tempfile.mkdtemp(prefix=stamp, dir=BACKUPS))
        manifest = {}
        for path, previous in ((CONFIG, prior_config), (HOOK, prior_hook)):
            manifest[str(path)] = {"existed": previous.content is not None, "mode": oct(previous.mode)}
            if previous.content is not None:
                (backup / path.name).write_bytes(previous.content)
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Configuration backup: {backup}", flush=True)

        (WEBROOT / ".well-known/acme-challenge").mkdir(parents=True, exist_ok=True, mode=0o755)
        certificate = Path("/etc/letsencrypt/live") / domain
        existing_tls = (certificate / "fullchain.pem").is_file() and (certificate / "privkey.pem").is_file()
        # On repeated runs keep existing HTTPS working while ACME is checked.
        install_config(
            render_config(domain, tls=existing_tls), nginx, systemctl,
            verify=lambda: verify_http_challenge(domain),
        )
        command = [
            certbot, "certonly", "--webroot", "--webroot-path", str(WEBROOT),
            "--cert-name", domain, "--domain", domain, "--agree-tos", "--non-interactive",
            "--server", "https://acme-v02.api.letsencrypt.org/directory",
            "--keep-until-expiring",
        ]
        command += ["--email", email] if email else ["--register-unsafely-without-email"]
        run(command)
        if not (certificate / "fullchain.pem").is_file() or not (certificate / "privkey.pem").is_file():
            raise RuntimeError("Certbot returned without the expected certificate files")

        install_config(
            render_config(domain, tls=True), nginx, systemctl,
            verify=lambda: await_verification(lambda: verify_https(domain)),
        )
        hook = render_hook(domain, nginx, systemctl)
        compile(hook, str(HOOK), "exec")
        atomic_write(HOOK, hook.encode(), 0o755)
        run([systemctl, "enable", "--now", "nginx"], timeout=30)
        run([systemctl, "enable", "--now", "certbot.timer"], timeout=30)
        # A dry run uses the CA staging service. --run-deploy-hooks verifies the
        # hook against the active production certificate without replacing it.
        renewal_started_ns = time.time_ns()
        run([
            certbot, "renew", "--cert-name", domain, "--dry-run", "--run-deploy-hooks",
            "--non-interactive", "--no-random-sleep-on-renew",
        ])
        verify_renewal_hook(domain, since_ns=renewal_started_ns)
        await_verification(lambda: verify_https(domain))
        run([systemctl, "is-enabled", "certbot.timer"], timeout=30)
        run([systemctl, "is-active", "certbot.timer"], timeout=30)
        print(json.dumps({
            "domain": domain,
            "websocket_url": f"wss://{domain}/ws/<live_id>",
            "certificate_directory": str(certificate),
            "renewal_dry_run": "passed",
            "backup_directory": str(backup),
        }, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", type=domain_name)
    parser.add_argument("--email", type=email_address)
    parser.add_argument("--print-config", action="store_true", help="Only render HTTPS config; perform no changes")
    arguments = parser.parse_args()
    if arguments.print_config:
        print(render_config(arguments.domain, tls=True))
        return
    try:
        configure(arguments.domain, arguments.email)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Configuration incomplete: {exc}\n")


if __name__ == "__main__":
    main()
