#!/usr/bin/env python3
"""
nexvue-tls.py — inspect / validate / install station TLS PEMs and Lego config.

Used by nexvue-ops-tls.sh (Settings → Certificates). Stdlib + openssl CLI only.

Commands:
  status              JSON: installed cert + config + job (no private key)
  config              stdin JSON {email, domain?} → nexvue.env
                      Domain is NEXVUE_PUBLIC_HOSTNAME (legacy: body domain
                      or existing NEXVUE_TLS_DOMAIN). Writes TLS_DOMAIN as
                      a write-through alias of that name.
  validate            stdin JSON {cert, key} → inspect + match (no write)
  install             stdin JSON {cert, key, source} → atomic install
  deploy              copy lego hook cert+key into /etc/nexvue/tls
                      (LEGO_HOOK_CERT_* from lego v5; LEGO_CERT_* v4 fallback;
                      else <lego>/certificates/<public-hostname>.{crt,key})
  should-renew        JSON {renew: bool} — Lego cert within --days (default 30)

Env overrides (tests):
  NEXVUE_TLS_DIR NEXVUE_TLS_CERT NEXVUE_TLS_KEY NEXVUE_TLS_STATE_DIR
  NEXVUE_STATION_ENV NEXVUE_LEGO NEXVUE_LEGO_PATH NEXVUE_TLS_RENEW_DAYS
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z0-9.-]+$")
ASSIGN_RE = re.compile(r"^(\s*)(#?)(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
UNQUOTED_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:+=,@-]+$")
PEM_BEGIN = b"-----BEGIN "
PEM_MAX = 131072
RENEW_DAYS_DEFAULT = 30

EMAIL_KEY = "NEXVUE_TLS_EMAIL"
DOMAIN_KEY = "NEXVUE_TLS_DOMAIN"
PUBLIC_HOST_KEY = "NEXVUE_PUBLIC_HOSTNAME"


def tls_dir() -> Path:
    return Path(os.environ.get("NEXVUE_TLS_DIR", "/etc/nexvue/tls"))


def cert_path() -> Path:
    override = os.environ.get("NEXVUE_TLS_CERT")
    if override:
        return Path(override)
    return tls_dir() / "fullchain.pem"


def key_path() -> Path:
    override = os.environ.get("NEXVUE_TLS_KEY")
    if override:
        return Path(override)
    return tls_dir() / "privkey.pem"


def state_dir() -> Path:
    return Path(os.environ.get("NEXVUE_TLS_STATE_DIR", "/var/lib/nexvue/tls"))


def source_path() -> Path:
    return state_dir() / "source"


def job_path() -> Path:
    return state_dir() / "job.json"


def backup_dir() -> Path:
    return state_dir() / "backup"


def station_env() -> Path:
    return Path(os.environ.get("NEXVUE_STATION_ENV", "/etc/nexvue/nexvue.env"))


def lego_bin() -> Path:
    return Path(os.environ.get("NEXVUE_LEGO", "/usr/local/bin/lego"))


def lego_store_dir() -> Path:
    return Path(os.environ.get("NEXVUE_LEGO_PATH", "/var/lib/nexvue/lego"))


def lego_deploy_paths() -> tuple[Path, Path]:
    """Resolve the lego-issued pair for the deploy hook.

    Lego v5 sets LEGO_HOOK_CERT_PATH / LEGO_HOOK_CERT_KEY_PATH. v4 used
    LEGO_CERT_PATH / LEGO_CERT_KEY_PATH. If the hook env is missing, fall
    back to the on-disk store using Public hostname (Settings-owned).
    """
    cert = (
        os.environ.get("LEGO_HOOK_CERT_PATH")
        or os.environ.get("LEGO_CERT_PATH")
        or ""
    ).strip()
    key = (
        os.environ.get("LEGO_HOOK_CERT_KEY_PATH")
        or os.environ.get("LEGO_CERT_KEY_PATH")
        or ""
    ).strip()
    if cert and key:
        return Path(cert), Path(key)
    try:
        domain = resolve_domain()
    except ValueError:
        domain = ""
    if domain:
        base = lego_store_dir() / "certificates"
        crt = base / f"{domain}.crt"
        keyp = base / f"{domain}.key"
        if crt.is_file() and keyp.is_file():
            return crt, keyp
    raise ValueError("lego did not pass certificate paths")


def renew_days() -> int:
    raw = os.environ.get("NEXVUE_TLS_RENEW_DAYS", str(RENEW_DAYS_DEFAULT))
    try:
        n = int(raw)
    except ValueError:
        return RENEW_DAYS_DEFAULT
    return n if n > 0 else RENEW_DAYS_DEFAULT


def json_out(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, separators=(",", ":")))
    return code


def fail(msg: str, code: int = 1) -> int:
    return json_out({"ok": False, "error": msg}, code)


def openssl() -> str:
    exe = shutil.which("openssl")
    if not exe:
        raise RuntimeError("openssl is required")
    return exe


def run_openssl(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [openssl(), *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def sanitize_domain(raw: str, *, required: bool = False) -> str:
    value = raw.strip().lower()
    if value == "":
        if required:
            raise ValueError("Enter a hostname like nexvue.example.com")
        return ""
    if "://" in value or "/" in value or ":" in value:
        raise ValueError("Enter a hostname like nexvue.example.com")
    if _looks_ipv4(value):
        raise ValueError("Let's Encrypt needs a DNS name, not an IP")
    if len(value) > 253 or not HOSTNAME_RE.fullmatch(value):
        raise ValueError("Enter a hostname like nexvue.example.com")
    if "." not in value:
        raise ValueError("Use a public DNS name (example: nexvue.example.com)")
    return value


def sanitize_email(raw: str, *, required: bool = False) -> str:
    value = raw.strip()
    if value == "":
        if required:
            raise ValueError("Enter an email for Let's Encrypt notices")
        return ""
    if len(value) > 254 or not EMAIL_RE.fullmatch(value):
        raise ValueError("Enter a valid email address")
    if any(ch in value for ch in "\"'`$;|&<>\\"):
        raise ValueError("Enter a valid email address")
    return value


def _looks_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def decode_pem_field(raw: str, what: str) -> bytes:
    text = (raw or "").strip()
    if text == "":
        raise ValueError(f"{what} is required")
    if text.startswith("data:"):
        comma = text.find(",")
        if comma < 0:
            raise ValueError(f"{what} is not valid PEM")
        text = text[comma + 1 :]
    if "-----BEGIN " in text:
        pem = text.replace("\r\n", "\n").encode("ascii", errors="strict")
    else:
        try:
            pem = base64.b64decode(text, validate=False)
        except (ValueError, binascii_error()) as exc:
            raise ValueError(f"{what} is not valid PEM") from exc
        if PEM_BEGIN not in pem:
            raise ValueError(f"{what} is not a PEM file")
    if len(pem) > PEM_MAX:
        raise ValueError(f"{what} exceeds 128 KB")
    if PEM_BEGIN not in pem:
        raise ValueError(f"{what} is not a PEM file")
    return pem


def binascii_error() -> type[Exception]:
    import binascii

    return binascii.Error


def _env_value(text: str, key: str) -> str:
    for line in text.splitlines():
        m = ASSIGN_RE.match(line)
        if not m or m.group(2):
            continue
        if m.group(4) == key:
            val = m.group(5).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            return val
    return ""


def read_env_map(path: Path | None = None) -> dict[str, str]:
    p = path or station_env()
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    return {
        "email": _env_value(text, EMAIL_KEY),
        "domain": _env_value(text, DOMAIN_KEY),
        "public_hostname": _env_value(text, PUBLIC_HOST_KEY),
    }


def format_assignment_value(value: str) -> str:
    if value == "" or UNQUOTED_SAFE_RE.match(value):
        return value
    return f'"{value}"'


def apply_env_patch(text: str, email: str, domain: str) -> str:
    pending = {EMAIL_KEY: email, DOMAIN_KEY: domain}
    lines = text.splitlines(keepends=True)
    new_lines: list[str] = []

    def line_ending(line: str) -> str:
        return "\r\n" if line.endswith("\r\n") else "\n"

    for line in lines:
        m = ASSIGN_RE.match(line.rstrip("\r\n"))
        if not m:
            new_lines.append(line)
            continue
        indent, hashmark, _sp, key, _old = m.groups()
        ending = line_ending(line)
        if key in pending and not hashmark:
            val = format_assignment_value(pending.pop(key))
            new_lines.append(f"{indent}{key}={val}{ending}")
            continue
        new_lines.append(line)

    if pending:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        if not any("# --- Certificates" in ln for ln in new_lines):
            if new_lines and new_lines[-1].strip():
                new_lines.append("\n")
            new_lines.append("# --- Certificates (written by Settings) ---\n")
        for key in (EMAIL_KEY, DOMAIN_KEY):
            if key in pending:
                new_lines.append(f"{key}={format_assignment_value(pending[key])}\n")
    return "".join(new_lines)


def resolve_domain(cfg: dict[str, str] | None = None) -> str:
    """Public hostname is the station DNS name; TLS_DOMAIN is a legacy alias."""
    cfg = cfg or read_env_map()
    try:
        hostname = sanitize_domain(cfg.get("public_hostname") or "")
    except ValueError:
        hostname = ""
    if hostname:
        return hostname
    return sanitize_domain(cfg.get("domain") or "")


def inspect_pem(cert_pem: bytes) -> dict:
    proc = run_openssl(
        [
            "x509",
            "-noout",
            "-subject",
            "-issuer",
            "-startdate",
            "-enddate",
            "-nameopt",
            "RFC2253",
            "-ext",
            "subjectAltName",
        ],
        stdin=cert_pem,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(err or "certificate is not a valid X.509 PEM")
    text = proc.stdout.decode("utf-8", errors="replace")
    info = {
        "subject": "",
        "issuer": "",
        "not_before": "",
        "not_after": "",
        "sans": [],
    }
    sans: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("subject="):
            info["subject"] = line.split("=", 1)[1].strip()
        elif line.lower().startswith("issuer="):
            info["issuer"] = line.split("=", 1)[1].strip()
        elif line.lower().startswith("notbefore="):
            info["not_before"] = line.split("=", 1)[1].strip()
        elif line.lower().startswith("notafter="):
            info["not_after"] = line.split("=", 1)[1].strip()
        elif line.lower().startswith("dns:") or line.lower().startswith("ip address:"):
            for part in line.split(","):
                part = part.strip()
                if part.lower().startswith("dns:"):
                    sans.append(part.split(":", 1)[1].strip())
                elif part.lower().startswith("ip address:"):
                    sans.append(part.split(":", 1)[1].strip())
    info["sans"] = sans
    end_epoch = _asn1_to_epoch(info["not_after"])
    now = int(time.time())
    info["days_left"] = None if end_epoch is None else int((end_epoch - now) / 86400)
    info["expired"] = bool(end_epoch is not None and end_epoch <= now)
    info["self_signed"] = _is_self_signed(info["subject"], info["issuer"])
    info["source_guess"] = classify_source(info["issuer"], info["self_signed"], None)
    info["leaf_count"] = cert_pem.count(b"-----BEGIN CERTIFICATE-----")
    return info


def _asn1_to_epoch(raw: str) -> int | None:
    # OpenSSL -enddate: "Nov 18 12:00:00 2026 GMT"
    cleaned = re.sub(r"\s+", " ", (raw or "").strip())
    cleaned = cleaned.replace(" GMT", "").replace(" UTC", "")
    if not cleaned:
        return None
    try:
        t = time.strptime(cleaned, "%b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return _utc_epoch(t)


def _utc_epoch(t: time.struct_time) -> int:
    # calendar.timegm is stdlib and UTC-correct.
    import calendar

    return int(calendar.timegm(t))


def _is_self_signed(subject: str, issuer: str) -> bool:
    if subject and issuer and subject == issuer:
        return True
    return "O=NexVUE" in issuer.replace(" ", "") or "O = NexVUE" in issuer


def classify_source(issuer: str, self_signed: bool, stamp: str | None) -> str:
    if stamp in ("lego", "upload", "self-signed"):
        return stamp
    low = (issuer or "").lower()
    if "let's encrypt" in low or "lets encrypt" in low:
        return "lego"
    if self_signed:
        return "self-signed"
    return "upload" if stamp == "upload" else ("self-signed" if self_signed else "unknown")


def read_source_stamp() -> str:
    p = source_path()
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0][:32]


def write_source_stamp(source: str) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    source_path().write_text(source + "\n", encoding="utf-8")
    try:
        source_path().chmod(0o644)
    except OSError:
        pass


def pubkey_fingerprint(args: list[str], stdin: bytes | None = None) -> str:
    pub = run_openssl(args, stdin=stdin)
    if pub.returncode != 0:
        raise ValueError("could not read public key")
    der = run_openssl(["pkey", "-pubin", "-outform", "der"], stdin=pub.stdout)
    if der.returncode != 0:
        raise ValueError("could not convert public key")
    import hashlib

    return hashlib.sha256(der.stdout).hexdigest()


def key_matches_cert(cert_pem: bytes, key_pem: bytes) -> bool:
    cert_fp = pubkey_fingerprint(["x509", "-noout", "-pubkey"], stdin=cert_pem)
    key_fp = pubkey_fingerprint(["pkey", "-pubout"], stdin=key_pem)
    return cert_fp == key_fp


def validate_pair(cert_pem: bytes, key_pem: bytes, *, allow_expired: bool = False) -> dict:
    if b"PRIVATE KEY" not in key_pem and b"RSA PRIVATE KEY" not in key_pem:
        raise ValueError("private key is not a PEM key")
    info = inspect_pem(cert_pem)
    if info["expired"] and not allow_expired:
        raise ValueError("certificate has expired")
    if not key_matches_cert(cert_pem, key_pem):
        raise ValueError("private key does not match the certificate")
    info["ok"] = True
    info["chain_complete"] = info["leaf_count"] >= 2
    return info


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    try:
        tmp.chmod(mode)
    except OSError:
        pass
    tmp.replace(path)


def _chown_ssl_cert(paths: list[Path]) -> None:
    if os.name != "posix":
        return
    try:
        import grp
        import pwd

        uid = pwd.getpwnam("root").pw_uid
        try:
            gid = grp.getgrnam("ssl-cert").gr_gid
        except KeyError:
            gid = pwd.getpwnam("root").pw_gid
        for p in paths:
            os.chown(p, uid, gid)
    except (KeyError, OSError, PermissionError):
        pass


def backup_live_pair() -> bool:
    cert = cert_path()
    key = key_path()
    if not (cert.is_file() and key.is_file()):
        return False
    dest = backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cert, dest / "fullchain.pem")
    shutil.copy2(key, dest / "privkey.pem")
    try:
        (dest / "fullchain.pem").chmod(0o644)
        (dest / "privkey.pem").chmod(0o640)
    except OSError:
        pass
    _chown_ssl_cert([dest / "fullchain.pem", dest / "privkey.pem"])
    return True


def install_pair(cert_pem: bytes, key_pem: bytes, source: str) -> dict:
    if source not in ("lego", "upload"):
        raise ValueError("source must be lego or upload")
    info = validate_pair(cert_pem, key_pem)
    backup_live_pair()
    cert = cert_path()
    key = key_path()
    cert.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(cert, cert_pem if cert_pem.endswith(b"\n") else cert_pem + b"\n", 0o644)
    _atomic_write(key, key_pem if key_pem.endswith(b"\n") else key_pem + b"\n", 0o640)
    _chown_ssl_cert([cert, key])
    write_source_stamp(source)
    info["source"] = source
    info["installed"] = True
    return info


def deploy_from_lego() -> dict:
    cert_file, key_file = lego_deploy_paths()
    cert_pem = cert_file.read_bytes()
    key_pem = key_file.read_bytes()
    return install_pair(cert_pem, key_pem, "lego")


def inspect_installed() -> dict:
    cert = cert_path()
    key = key_path()
    out: dict = {
        "ok": True,
        "present": False,
        "subject": "",
        "issuer": "",
        "not_before": "",
        "not_after": "",
        "days_left": None,
        "sans": [],
        "expired": False,
        "self_signed": False,
        "source": "missing",
        "chain_complete": False,
    }
    if not (cert.is_file() and key.is_file()):
        return out
    pem = cert.read_bytes()
    info = inspect_pem(pem)
    stamp = read_source_stamp()
    out.update(info)
    out["ok"] = True
    out["present"] = True
    out["source"] = classify_source(info["issuer"], info["self_signed"], stamp or None)
    out["chain_complete"] = info["leaf_count"] >= 2
    return out


def read_job() -> dict:
    p = job_path()
    empty = {
        "busy": False,
        "action": "",
        "ok": None,
        "error": "",
        "started_at": 0,
        "finished_at": 0,
    }
    if not p.is_file():
        return empty
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    empty.update({k: data[k] for k in empty if k in data})
    return empty


def write_job(fields: dict) -> dict:
    cur = read_job()
    cur.update(fields)
    state_dir().mkdir(parents=True, exist_ok=True)
    job_path().write_text(json.dumps(cur, separators=(",", ":")) + "\n", encoding="utf-8")
    try:
        job_path().chmod(0o644)
    except OSError:
        pass
    return cur


def lego_info() -> dict:
    path = lego_bin()
    if not path.is_file():
        return {"lego_installed": False, "lego_version": ""}
    proc = subprocess.run(
        [str(path), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    ver = (proc.stdout or "").strip().splitlines()[0] if proc.returncode == 0 else ""
    m = re.search(r"(\d+\.\d+\.\d+)", ver)
    return {"lego_installed": proc.returncode == 0, "lego_version": m.group(1) if m else ver}


def should_renew(info: dict | None = None, cfg: dict[str, str] | None = None) -> tuple[bool, str]:
    cfg = cfg or read_env_map()
    email = sanitize_email(cfg.get("email") or "")
    domain = resolve_domain(cfg)
    if not email or not domain:
        return False, "email and domain are not configured"
    info = info or inspect_installed()
    if not info.get("present"):
        return False, "no certificate installed"
    if info.get("source") != "lego":
        return False, "installed certificate is not from Let's Encrypt"
    days = info.get("days_left")
    if days is None:
        return False, "could not read certificate expiry"
    threshold = renew_days()
    if int(days) > threshold:
        return False, f"not due ({days} days left, renew at {threshold})"
    return True, "due"


def _safe_domain(raw: str) -> str:
    try:
        return sanitize_domain(raw or "")
    except ValueError:
        return ""


def _safe_email(raw: str) -> str:
    try:
        return sanitize_email(raw or "")
    except ValueError:
        return ""


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = read_env_map()
    info = inspect_installed()
    job = read_job()
    renew, renew_reason = should_renew(info, cfg)
    try:
        resolved = resolve_domain(cfg)
    except ValueError:
        resolved = ""
    payload = {
        **info,
        "email": _safe_email(cfg.get("email") or ""),
        "domain": _safe_domain(cfg.get("domain") or ""),
        "public_hostname": _safe_domain(cfg.get("public_hostname") or ""),
        "resolved_domain": resolved,
        "job": job,
        "busy": bool(job.get("busy")),
        "renew_due": renew,
        "renew_reason": renew_reason,
        **lego_info(),
        "challenge": "tls-alpn-01",
    }
    return json_out(payload)


def cmd_config(_args: argparse.Namespace) -> int:
    try:
        body = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        return fail(f"invalid JSON: {exc}")
    if not isinstance(body, dict):
        return fail("body must be a JSON object")
    try:
        email = sanitize_email(str(body.get("email", "")), required=True)
        cfg = read_env_map()
        domain = sanitize_domain(cfg.get("public_hostname") or "")
        if domain == "":
            domain = sanitize_domain(str(body.get("domain", "")))
        if domain == "":
            domain = sanitize_domain(cfg.get("domain") or "")
        if domain == "":
            raise ValueError("Set a Public hostname under Settings → Public reachability")
        domain = sanitize_domain(domain, required=True)
    except ValueError as exc:
        return fail(str(exc))
    env = station_env()
    text = env.read_text(encoding="utf-8", errors="replace") if env.is_file() else ""
    new = apply_env_patch(text, email, domain)
    env.parent.mkdir(parents=True, exist_ok=True)
    tmp = env.with_suffix(env.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(env)
    try:
        env.chmod(0o644)
    except OSError:
        pass
    return json_out({"ok": True, "email": email, "domain": domain})


def _read_pair_body() -> tuple[bytes, bytes]:
    body = json.load(sys.stdin)
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    cert_pem = decode_pem_field(str(body.get("cert", "")), "certificate")
    key_pem = decode_pem_field(str(body.get("key", "")), "private key")
    return cert_pem, key_pem


def cmd_validate(_args: argparse.Namespace) -> int:
    try:
        cert_pem, key_pem = _read_pair_body()
        info = validate_pair(cert_pem, key_pem)
    except (json.JSONDecodeError, ValueError) as exc:
        return fail(str(exc))
    info["ok"] = True
    return json_out(info)


def cmd_install(_args: argparse.Namespace) -> int:
    try:
        body = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        return fail(f"invalid JSON: {exc}")
    if not isinstance(body, dict):
        return fail("body must be a JSON object")
    try:
        cert_pem = decode_pem_field(str(body.get("cert", "")), "certificate")
        key_pem = decode_pem_field(str(body.get("key", "")), "private key")
        source = str(body.get("source") or "upload")
        info = install_pair(cert_pem, key_pem, source)
    except ValueError as exc:
        return fail(str(exc))
    info["ok"] = True
    return json_out(info)


def cmd_deploy(_args: argparse.Namespace) -> int:
    try:
        info = deploy_from_lego()
    except (OSError, ValueError) as exc:
        return fail(str(exc))
    info["ok"] = True
    return json_out(info)


def cmd_should_renew(_args: argparse.Namespace) -> int:
    renew, reason = should_renew()
    return json_out({"ok": True, "renew": renew, "reason": reason})


def cmd_job(_args: argparse.Namespace) -> int:
    try:
        body = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except json.JSONDecodeError as exc:
        return fail(f"invalid JSON: {exc}")
    if body:
        if not isinstance(body, dict):
            return fail("body must be a JSON object")
        return json_out({"ok": True, "job": write_job(body)})
    return json_out({"ok": True, "job": read_job()})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nexvue-tls.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("config")
    sub.add_parser("validate")
    sub.add_parser("install")
    sub.add_parser("deploy")
    sub.add_parser("should-renew")
    sub.add_parser("job")
    args = ap.parse_args(argv)
    cmds = {
        "status": cmd_status,
        "config": cmd_config,
        "validate": cmd_validate,
        "install": cmd_install,
        "deploy": cmd_deploy,
        "should-renew": cmd_should_renew,
        "job": cmd_job,
    }
    try:
        return cmds[args.cmd](args)
    except RuntimeError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
