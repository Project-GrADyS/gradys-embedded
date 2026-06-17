"""Ephemeral TLS certificate generation for the QUIC/HTTP3 message API.

QUIC mandates TLS 1.3, so the message-API server cannot bind without presenting a
certificate. When the operator does not supply one via ``RunnerConfiguration``, the runner
generates a throwaway self-signed certificate at boot. It is written to temporary files for
Hypercorn to read and is never reused across runs nor used as a client trust anchor — peer
verification stays disabled unless an explicit ``certfile`` is configured.
"""

import datetime
import ipaddress
import tempfile


def generate_self_signed_cert() -> tuple[str, str]:
    """Generate an ephemeral self-signed cert/key pair and return their file paths.

    The files live for the lifetime of the process (temp dir); they are intentionally not
    cached or cleaned up, mirroring the throwaway nature of the dev certificate.
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "gradys-embedded-dev"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.IPAddress(ipaddress.IPv4Address("0.0.0.0")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    cert_fd, cert_path = tempfile.mkstemp(prefix="gradys-embedded-", suffix="-cert.pem")
    key_fd, key_path = tempfile.mkstemp(prefix="gradys-embedded-", suffix="-key.pem")
    with open(cert_fd, "wb") as f:
        f.write(cert_bytes)
    with open(key_fd, "wb") as f:
        f.write(key_bytes)

    return cert_path, key_path
