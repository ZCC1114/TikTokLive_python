import argparse
import json
import subprocess
from types import SimpleNamespace

import pytest

from deploy import configure_nginx as nginx


@pytest.mark.parametrize("domain", [
    "live.example.com;reload", "live.example.com\nserver{}", "$(id).example.com", "*.example.com",
    "-live.example.com", "live..example.com", "localhost", "163.7.2.127", "直播.example.com",
    "a" * 64 + ".example.com", "live.example.com..",
])
def test_domain_rejects_shell_nginx_injection_and_invalid_names(domain):
    with pytest.raises(argparse.ArgumentTypeError):
        nginx.domain_name(domain)


def test_config_preserves_business_path_and_restricts_diagnostics():
    domain = nginx.domain_name("Live.Example.COM.")
    configuration = nginx.render_config(domain, tls=True)
    assert "server_name live.example.com;" in configuration
    assert "proxy_pass http://127.0.0.1:8765;" in configuration
    assert "proxy_http_version 1.1;" in configuration
    assert "proxy_buffering off;" in configuration
    assert "proxy_read_timeout 3600s;" in configuration
    assert "deny all;" in configuration.split("location = /readyz", 1)[1].split("}", 1)[0]
    assert "ssl_certificate " not in nginx.render_config(domain, tls=False)
    assert "return 503;" in nginx.render_config(domain, tls=False)
    assert "return 308" not in nginx.render_config(domain, tls=False)


@pytest.mark.parametrize("original", [None, f"# {nginx.MARKER}\n# previous configuration\n"])
def test_nginx_validation_failure_restores_last_configuration(tmp_path, monkeypatch, original):
    path = tmp_path / "tiktok-live.conf"
    monkeypatch.setattr(nginx, "CONFIG", path)
    if original is not None:
        path.write_text(original)

    def command(arguments, **kwargs):
        if kwargs.get("capture_output"):
            raise subprocess.CalledProcessError(1, arguments)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(nginx.subprocess, "run", command)
    with pytest.raises(subprocess.CalledProcessError):
        nginx.install_config(f"# {nginx.MARKER}\ninvalid syntax", "nginx", "systemctl")
    assert path.read_text() == original if original is not None else not path.exists()


def test_refuses_to_overwrite_unmanaged_site(tmp_path, monkeypatch):
    path = tmp_path / "tiktok-live.conf"
    path.write_text("# another service owns this file\n")
    monkeypatch.setattr(nginx, "CONFIG", path)
    with pytest.raises(RuntimeError, match="unmanaged"):
        nginx.install_config("replacement", "nginx", "systemctl")
    assert path.read_text() == "# another service owns this file\n"


def test_dns_rejects_extra_destinations_and_accepts_only_expected_ipv4(monkeypatch):
    monkeypatch.setattr(nginx.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, (nginx.EXPECTED_IPV4, 80))])
    nginx.verify_dns("live.example.com")
    monkeypatch.setattr(nginx.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("::1", 80))])
    with pytest.raises(RuntimeError, match="DNS must resolve"):
        nginx.verify_dns("live.example.com")


def test_renew_hook_compiles_and_is_scoped_to_one_certificate():
    hook = nginx.render_hook("live.example.com", "/usr/sbin/nginx", "/usr/bin/systemctl")
    compile(hook, "deploy-hook", "exec")
    assert 'os.environ.get("RENEWED_LINEAGE")' in hook
    assert "/etc/letsencrypt/live/live.example.com" in hook
    assert "shell=" not in hook


def test_post_reload_probe_failure_restores_previous_configuration(tmp_path, monkeypatch):
    path = tmp_path / "tiktok-live.conf"
    original = f"# {nginx.MARKER}\n# existing HTTP configuration\n"
    path.write_text(original)
    monkeypatch.setattr(nginx, "CONFIG", path)
    monkeypatch.setattr(
        nginx.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="syntax is ok"),
    )

    def failed_probe():
        raise RuntimeError("HTTPS not ready")

    with pytest.raises(RuntimeError, match="HTTPS not ready"):
        nginx.install_config(f"# {nginx.MARKER}\n# new HTTPS", "nginx", "systemctl", verify=failed_probe)
    assert path.read_text() == original


def test_reload_readiness_probe_retries_transient_refusal_then_stops(monkeypatch):
    waits = []
    monkeypatch.setattr(nginx.time, "sleep", waits.append)
    calls = 0

    def probe():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionRefusedError("nginx workers are reloading")

    nginx.await_verification(probe)
    assert calls == 3 and len(waits) == 2


def test_reload_readiness_probe_propagates_failure_at_deadline():
    def probe():
        raise RuntimeError("wrong server")

    with pytest.raises(RuntimeError, match="wrong server"):
        nginx.await_verification(probe, timeout=0)


@pytest.mark.parametrize("healthy", [True, False])
def test_http01_probe_checks_host_and_removes_test_token(tmp_path, monkeypatch, healthy):
    challenge_dir = tmp_path / ".well-known/acme-challenge"
    challenge_dir.mkdir(parents=True)
    monkeypatch.setattr(nginx, "WEBROOT", tmp_path)
    monkeypatch.setattr(nginx, "await_verification", lambda probe: probe())
    requests = []
    closed = []

    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 80

        def request(self, method, path, headers):
            requests.append((method, path, headers))

        def getresponse(self):
            return SimpleNamespace(
                status=200 if healthy else 404,
                read=lambda size: b"tiktok-live-acme-preflight",
            )

        def close(self):
            closed.append(True)

    monkeypatch.setattr(nginx.http.client, "HTTPConnection", Connection)
    if healthy:
        nginx.verify_http_challenge("live.example.com")
    else:
        with pytest.raises(RuntimeError, match="webroot"):
            nginx.verify_http_challenge("live.example.com")
    assert requests[0][2] == {"Host": "live.example.com"}
    assert requests[0][1].startswith("/.well-known/acme-challenge/preflight-")
    assert not list(challenge_dir.iterdir())
    assert closed == [True]


@pytest.mark.parametrize("receipt", [
    None,
    {},
    {"lineage": "/etc/letsencrypt/live/live.example.com", "completed_at_ns": 99},
    {"lineage": "/etc/letsencrypt/live/other.example.com", "completed_at_ns": 101},
    {"lineage": "/etc/letsencrypt/live/live.example.com", "completed_at_ns": "101"},
])
def test_dry_run_requires_fresh_successful_deploy_hook_evidence(tmp_path, monkeypatch, receipt):
    path = tmp_path / "hook-receipt.json"
    monkeypatch.setattr(nginx, "HOOK_RECEIPT", path)
    if receipt is not None:
        path.write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="deploy-hook"):
        nginx.verify_renewal_hook("live.example.com", since_ns=100)


@pytest.mark.parametrize("reload_succeeds", [True, False])
def test_deploy_hook_only_receipts_after_successful_nginx_reload(tmp_path, monkeypatch, reload_succeeds):
    path = tmp_path / "hook-receipt.json"
    monkeypatch.setattr(nginx, "HOOK_RECEIPT", path)
    monkeypatch.setenv("RENEWED_LINEAGE", "/etc/letsencrypt/live/live.example.com")
    commands = []

    def run(arguments, **kwargs):
        commands.append(arguments)
        if not reload_succeeds and "reload" in arguments:
            raise subprocess.CalledProcessError(1, arguments)

    monkeypatch.setattr(nginx.subprocess, "run", run)
    code = compile(nginx.render_hook("live.example.com", "nginx", "systemctl"), "hook", "exec")
    if reload_succeeds:
        exec(code, {})
        nginx.verify_renewal_hook("live.example.com", since_ns=0)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            exec(code, {})
        assert not path.exists()
    assert commands == [["nginx", "-t"], ["systemctl", "reload", "nginx"]]
