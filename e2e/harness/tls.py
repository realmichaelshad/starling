"""A throwaway certificate for the fake Stripe's hosted pages.

Stripe's Checkout and Portal URLs are https, and the gateway refuses a session whose URL is
not (`billing/service.py: _https`), so the fake serves its pages over TLS on a second port.
The certificate is generated per run for 127.0.0.1, loaded into an SSL context, and its files
are removed again straight away: nothing here is a credential for anything, and the key never
outlives the process that made it.

Clients trust it by not verifying (`httpx.Client(verify=False)` in the journeys; Chrome with
`--ignore-certificate-errors` in part 2), which is what a local self-signed page deserves.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VALID_DAYS = 2


def generate(directory: Path, common_name: str = "starling-e2e-fake-stripe") -> tuple[bytes, bytes]:
    """(certificate PEM, private key PEM) for 127.0.0.1 and localhost."""
    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def server_context(directory: Path) -> ssl.SSLContext:
    """An SSL context for a fresh self-signed certificate; its files are removed after loading."""
    certificate_pem, key_pem = generate(directory)
    cert_path = directory / "fake-stripe-cert.txt"
    key_path = directory / "fake-stripe-key.txt"
    cert_path.write_bytes(certificate_pem)
    key_path.write_bytes(key_pem)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(str(cert_path), str(key_path))
    finally:
        for path in (cert_path, key_path):
            try:
                path.unlink()
            except OSError:  # a leftover in the git-ignored run folder is harmless
                pass
    return context
