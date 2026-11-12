"""Self-signed HTTPS certificate for the phone connection on your Wi-Fi.

Phones only allow the microphone on HTTPS pages, and the pairing code should
not travel in clear text, so the Wi-Fi side of the app is always HTTPS. The
certificate is self-signed: the phone shows a warning once, which you accept.
Validity is kept under 825 days and the certificate carries SAN and
serverAuth, because iOS refuses certificates without them.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
from pathlib import Path

VALID_DAYS = 800


def ensure_cert(directory: Path, ips: list[str]) -> tuple[Path, Path]:
    cert_path, key_path = directory / "phone-cert.pem", directory / "phone-key.pem"
    if cert_path.exists() and key_path.exists() and _still_valid(cert_path, ips):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "meetnotes")])
    now = dt.datetime.now(dt.timezone.utc)
    alt = [x509.DNSName("localhost")]
    for ip in sorted(set(ips) | {"127.0.0.1"}):
        alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    if os.name == "posix":
        os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _still_valid(cert_path: Path, ips: list[str]) -> bool:
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) < dt.timedelta(days=14):
            return False
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        return set(ips) <= have
    except Exception:
        return False


def fingerprint(cert_path: Path) -> str:
    """SHA-256 of the certificate (lowercase hex). The Android app pins this."""
    import hashlib
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
