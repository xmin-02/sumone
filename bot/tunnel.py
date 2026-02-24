"""Cloudflared tunnel management for file viewer."""
import os
import re
import subprocess
import threading
import time

from config import IS_WINDOWS, log

# cloudflared binary name / path
_CLOUDFLARED_NAMES = ["cloudflared", "cloudflared.exe"]


def _find_cloudflared():
    """Return the cloudflared command if available, else None."""
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    # Check bot directory first (portable install)
    for name in _CLOUDFLARED_NAMES:
        local = os.path.join(bot_dir, name)
        if os.path.isfile(local):
            return local
    # Check PATH
    for name in _CLOUDFLARED_NAMES:
        try:
            kw = {}
            if IS_WINDOWS:
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                [name, "--version"], capture_output=True, timeout=10, **kw,
            )
            if result.returncode == 0:
                return name
        except Exception:
            continue
    return None


def check_cloudflared():
    """Return True if cloudflared is available."""
    return _find_cloudflared() is not None


def install_cloudflared():
    """Download cloudflared to the bot directory. Returns True on success."""
    import urllib.request
    bot_dir = os.path.dirname(os.path.abspath(__file__))

    if IS_WINDOWS:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        dest = os.path.join(bot_dir, "cloudflared.exe")
    else:
        import platform
        machine = platform.machine().lower()
        if machine in ("aarch64", "arm64"):
            arch = "arm64"
        else:
            arch = "amd64"
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
        dest = os.path.join(bot_dir, "cloudflared")

    log.info("Downloading cloudflared from %s", url)
    try:
        urllib.request.urlretrieve(url, dest)
        if not IS_WINDOWS:
            os.chmod(dest, 0o755)
        log.info("cloudflared installed at %s", dest)
        return True
    except Exception as e:
        log.warning("Failed to install cloudflared: %s", e)
        return False


def start_tunnel(port, timeout=15):
    """Start a cloudflared quick tunnel pointing to localhost:port.

    Returns (process, public_url) or (None, None) on failure.
    """
    cmd_path = _find_cloudflared()
    if not cmd_path:
        return None, None

    cmd = [cmd_path, "tunnel", "--url", f"http://localhost:{port}"]
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(cmd, **kw)
    except Exception as e:
        log.warning("Failed to start cloudflared: %s", e)
        return None, None

    # cloudflared prints the public URL to stderr
    url_pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
    public_url = [None]

    def _read_stderr():
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace")
                m = url_pattern.search(line)
                if m:
                    public_url[0] = m.group(0)
                    return
        except Exception:
            pass

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()
    reader.join(timeout=timeout)

    if public_url[0]:
        log.info("Cloudflared tunnel ready: %s -> localhost:%d", public_url[0], port)
        # Keep draining stderr in background to prevent pipe buffer filling
        def _drain():
            try:
                for _ in proc.stderr:
                    pass
            except Exception:
                pass
        threading.Thread(target=_drain, daemon=True).start()
        return proc, public_url[0]

    log.warning("Cloudflared tunnel URL not found within %ds", timeout)
    stop_tunnel(proc)
    return None, None


def stop_tunnel(proc):
    """Terminate the cloudflared process."""
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info("Cloudflared tunnel stopped")
    except Exception as e:
        log.warning("Error stopping cloudflared: %s", e)
