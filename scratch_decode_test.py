#!/usr/bin/env python3
"""Prototype decoder for AprXorEnc ae1 wire format — validate against a real ext_enc blob."""
import base64, json, sys

SECRET_KEY = "6cxqx3vRwA41I8FvZFTjS55xWj5mjvVX2CfV0UP5ywgv0nZ6PoDUeH_it986sZWz"  # from AprXorEnc.swift, length 64

def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def decrypt(blob: str) -> bytes:
    ver, salt_len_s, rest = blob.split(":", 2)
    assert ver == "ae1", f"unexpected version {ver}"
    salt_len = int(salt_len_s)
    salt = rest[-salt_len:]
    ct = b64url_decode(rest[:-salt_len])
    key = (salt + SECRET_KEY).encode()
    return bytes(ct[i] ^ key[i % len(key)] for i in range(len(ct)))

if __name__ == "__main__":
    path = sys.argv[1]
    with open(path) as f:
        body = json.load(f)
    blob = body["ext_enc"]
    pt = decrypt(blob)
    print("=== decoded bytes len:", len(pt))
    obj = json.loads(pt)
    print("=== valid JSON, top-level keys:", list(obj.keys()))
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:2000])
