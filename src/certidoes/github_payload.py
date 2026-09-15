"""Encrypt/decrypt small Receita request payloads for GitHub Actions."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def encrypt(payload: dict, public_key_pem: str) -> str:
    key = serialization.load_pem_public_key(public_key_pem.encode())
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ciphertext = key.encrypt(
        plaintext,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None),
    )
    return base64.urlsafe_b64encode(ciphertext).decode()


def decrypt(ciphertext: str, private_key_pem: str) -> dict:
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    plaintext = key.decrypt(
        base64.urlsafe_b64decode(ciphertext.encode()),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None),
    )
    payload = json.loads(plaintext)
    if set(payload) != {"documento", "data_nascimento"}:
        raise ValueError("invalid Receita payload fields")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("encrypt", "decrypt"))
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--ciphertext")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "encrypt":
        public_key = os.environ["RECEITA_GITHUB_PUBLIC_KEY"]
        payload = json.loads(args.payload.read_text(encoding="utf-8"))
        value = encrypt(payload, public_key)
        args.output.write_text(value, encoding="ascii")
    else:
        private_key = os.environ["RECEITA_PAYLOAD_PRIVATE_KEY"]
        payload = decrypt(args.ciphertext or "", private_key)
        args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
