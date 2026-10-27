#!/usr/bin/env python3
"""
Free My TV for webOS - One-Command Installer
Automates discovery, pairing, and installation onto your LG Smart TV
"""

import os
import sys
import json
import socket
import urllib.request
import subprocess
import shutil
import threading
import queue
import time

# Windows' default console codepage can't encode the emoji in the success
# message below; UTF-8 output works on every platform this script runs on.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO_DIR, "app", "appinfo.json"), "r", encoding="utf-8") as _f:
    _appinfo = json.load(_f)
APP_ID = _appinfo["id"]
DIST_IPK = os.path.join(REPO_DIR, "dist", f"{APP_ID}_{_appinfo['version']}_all.ipk")

def discover_lg_tv():
    print("[*] Scanning local network for LG webOS TV...")
    msg = (
        'M-SEARCH * HTTP/1.1\r\n'
        'HOST: 239.255.255.250:1900\r\n'
        'MAN: "ssdp:discover"\r\n'
        'MX: 2\r\n'
        'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n'
    ).encode('utf-8')
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(2.5)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.sendto(msg, ('239.255.255.250', 1900))
        while True:
            data, addr = sock.recvfrom(4096)
            ip = addr[0]
            # Probe dev port 9922
            s = socket.socket()
            s.settimeout(0.3)
            if s.connect_ex((ip, 9922)) == 0:
                s.close()
                sock.close()
                return ip
            s.close()
    except Exception:
        pass
    sock.close()
    return None

def package_is_stale():
    """True when the package is missing or older than any file in app/."""
    if not os.path.exists(DIST_IPK):
        return True
    built_at = os.path.getmtime(DIST_IPK)
    for root, _, files in os.walk(os.path.join(REPO_DIR, "app")):
        for name in files:
            if os.path.getmtime(os.path.join(root, name)) > built_at:
                return True
    return False

def fetch_key(tv_ip, passphrase, raw_key, dec_key):
    """Downloads the TV's SSH key from the Key Server and stores a decrypted copy."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        print("[!] Missing Python package 'cryptography'. Install it with:")
        print(f"    {sys.executable} -m pip install cryptography")
        return False
    if not passphrase:
        passphrase = input(f"Enter the 6-character Passphrase shown in Developer Mode on TV ({tv_ip}): ").strip()

    print(f"[*] Downloading authentication key from http://{tv_ip}:9991/webos_rsa ...")
    try:
        with urllib.request.urlopen(f"http://{tv_ip}:9991/webos_rsa", timeout=5) as resp:
            key_data = resp.read()
    except Exception as e:
        print(f"[!] Failed to fetch key: {e}")
        print("[!] Make sure 'Key Server' is toggled ON in the Developer Mode app on the TV.")
        return False

    try:
        key = serialization.load_pem_private_key(key_data, password=passphrase.encode())
    except (ValueError, TypeError):
        print("[!] That passphrase didn't unlock the key. Check the 6 characters on the TV and try again.")
        return False
    fd = os.open(raw_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_data)
    os.chmod(raw_key, 0o600)
    unenc = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )
    # Private keys must be owner-only: other local users could otherwise read
    # them, and OpenSSH refuses to use a key with group/other permissions.
    fd = os.open(dec_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(unenc)
    os.chmod(dec_key, 0o600)
    print("[+] SSH Key saved & decrypted successfully.")
    return True

def install_app(tv_ip, passphrase=None):
    if package_is_stale():
        print("[*] Package missing or older than app/. Building first...")
        build_script = os.path.join(REPO_DIR, "scripts", "build.py")
        if subprocess.run([sys.executable, build_script]).returncode != 0 or not os.path.exists(DIST_IPK):
            print("[!] Build failed.")
            return False

    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    raw_key = os.path.join(ssh_dir, "webos_tv_rsa")
    dec_key = os.path.join(ssh_dir, "webos_tv_rsa_decrypted")

    # Download key from TV if not present
    fresh_key = False
    if not os.path.exists(dec_key):
        if not fetch_key(tv_ip, passphrase, raw_key, dec_key):
            return False
        fresh_key = True

    print(f"[*] Uploading Free My TV to TV ({tv_ip})...")
    # List-form args (no shell=True) throughout: on Windows, shell=True runs
    # the command through cmd.exe regardless of which shell launched Python,
    # and cmd.exe mangles the embedded double quotes in the JSON payloads
    # below before ssh.exe ever sees them. Passing argv as a list sidesteps
    # every local shell's quoting rules; the single remote command string
    # still reaches the TV's real POSIX shell exactly as written.
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa"]
    scp_args = ["scp", *ssh_opts, "-P", "9922", "-i", dec_key, DIST_IPK, f"prisoner@{tv_ip}:/media/developer/temp/app.ipk"]
    res = subprocess.run(scp_args)
    if res.returncode != 0 and not fresh_key:
        # A cached key stops working after Dev Mode is turned off and on again.
        print("[*] Upload failed with the saved key; fetching a fresh one from the TV...")
        if fetch_key(tv_ip, passphrase, raw_key, dec_key):
            res = subprocess.run(scp_args)
    if res.returncode != 0:
        print("[!] SCP upload failed. Check that Developer Mode is ON and the TV is on the same network.")
        return False

    # luna-send-pub prints nothing in "-n" mode without a controlling TTY, and many
    # TVs' sshd refuses to allocate one. Interactive mode ("-i") streams every reply
    # over a plain pipe and exits when stdin closes - the way LG's own ares-install
    # talks to the TV - so it works with or without a PTY on every platform.
    def luna_call(uri, payload, verdict, timeout):
        remote_cmd = f"luna-send-pub -i {uri} '{json.dumps(payload)}'"
        args = ["ssh", *ssh_opts, "-p", "9922", "-i", dec_key, f"prisoner@{tv_ip}", remote_cmd]
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()

        def pump():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)
        threading.Thread(target=pump, daemon=True).start()

        replies, result = [], None
        deadline = time.time() + timeout
        try:
            while result is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    break
                if line is None:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                replies.append(line)
                try:
                    result = verdict(json.loads(line))
                except ValueError:
                    pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return result, replies, proc.stderr.read() if proc.stderr else ""

    def install_verdict(reply):
        state = str((reply.get("details") or {}).get("state", "")) or str(reply.get("statusValue", ""))
        if reply.get("returnValue") is False or "FAILED" in state.upper():
            return False
        if "installed" in state.lower():
            return True
        return None

    print("[*] Installing package on TV...")
    install_payload = {"id": APP_ID, "ipkUrl": "/media/developer/temp/app.ipk", "subscribe": True}
    ok, replies, err = luna_call("luna://com.webos.appInstallService/dev/install", install_payload, install_verdict, 90)
    if not ok:
        print("[!] Install did not reach the 'installed' state. Raw output:")
        print("\n".join(replies) or "(empty)")
        if err:
            print(err)
        return False

    print("[*] Launching Free My TV...")
    launched, replies, err = luna_call("luna://com.webos.applicationManager/launch", {"id": APP_ID},
                                       lambda reply: reply.get("returnValue"), 15)
    if not launched:
        print("[!] Install succeeded but launch failed. Raw output:")
        print("\n".join(replies) or "(empty)")
        return False

    print("\n[+] SUCCESS! Free My TV is installed and running on your LG TV! Enjoy! 🎉")
    return True

if __name__ == "__main__":
    ip = sys.argv[1] if len(sys.argv) > 1 else None
    passphrase = sys.argv[2] if len(sys.argv) > 2 else None

    if not ip:
        ip = discover_lg_tv()
        if not ip:
            ip = input("Enter TV IP Address (shown in Developer Mode app): ").strip()

    sys.exit(0 if install_app(ip, passphrase) else 1)
