"""Immutable encrypted objects used by artifacts and Temporal payload offloading."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import zlib
from collections.abc import Sequence
from pathlib import Path

from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, DefaultFailureConverterWithEncodedAttributes, PayloadCodec

from .security import Security
from .settings import Settings

COMPRESSED_INLINE = b"modelmesh/encrypted-zlib-inline-v1"
MAX_COMPRESSED_INLINE_BYTES = 2 * 1024 * 1024


class ObjectStore:
    def __init__(self, settings: Settings, security: Security):
        self.settings = settings
        self.security = security
        self.directory = settings.data_dir / "objects"

    async def put(self, data: bytes) -> str:
        encoded = self.security.seal(data).encode()
        digest = self.security.object_id(data)
        await asyncio.to_thread(self._write, digest, encoded)
        return digest

    def _write(self, digest: str, data: bytes) -> None:
        parent = self.directory / digest[:2]
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = parent / digest
        descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(name, destination)
            except FileExistsError:
                pass
        finally:
            Path(name).unlink(missing_ok=True)

    async def get(self, digest: str) -> bytes:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Invalid object reference.")
        encoded = await asyncio.to_thread((self.directory / digest[:2] / digest).read_bytes)
        data = self.security.unseal(encoded.decode())
        if self.security.object_id(data) != digest:
            raise ValueError("Object integrity check failed.")
        return data


class EncryptedPayloadCodec(PayloadCodec):
    """Encrypt every payload; offload large ones before they enter Temporal history."""

    def __init__(
        self, security: Security, objects: ObjectStore, threshold: int = 64 * 1024,
        *, compress: bool = False,
    ):
        self.security = security
        self.objects = objects
        self.threshold = threshold
        self.compress = compress

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        result = []
        for payload in payloads:
            serialized = payload.SerializeToString()
            if len(serialized) >= self.threshold:
                digest = await self.objects.put(serialized)
                result.append(Payload(
                    metadata={"encoding": b"modelmesh/encrypted-reference-v1"},
                    data=json.dumps({"object": digest}).encode(),
                ))
            else:
                encoding = b"modelmesh/encrypted-inline-v1"
                if self.compress and 1024 <= len(serialized) <= MAX_COMPRESSED_INLINE_BYTES:
                    compressed = struct.pack(">I", len(serialized)) + zlib.compress(serialized, level=3)
                    # Keep high-entropy/small data in the existing format. Test
                    # offloading against the ORIGINAL size: a tiny reference
                    # must not turn back into a larger inline compressed blob.
                    if len(compressed) + 64 < len(serialized):
                        serialized, encoding = compressed, COMPRESSED_INLINE
                result.append(Payload(
                    metadata={"encoding": encoding},
                    data=self.security.seal(serialized).encode(),
                ))
        return result

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        result = []
        for payload in payloads:
            encoding = payload.metadata.get("encoding")
            if encoding == b"modelmesh/encrypted-inline-v1":
                raw = self.security.unseal(payload.data.decode())
            elif encoding == COMPRESSED_INLINE:
                raw = self._inflate(self.security.unseal(payload.data.decode()))
            elif encoding == b"modelmesh/encrypted-reference-v1":
                raw = await self.objects.get(json.loads(payload.data)["object"])
            else:
                result.append(payload)
                continue
            decoded = Payload()
            decoded.ParseFromString(raw)
            result.append(decoded)
        return result

    @staticmethod
    def _inflate(encoded: bytes) -> bytes:
        if len(encoded) < 4:
            raise ValueError("Invalid compressed Temporal payload.")
        expected, = struct.unpack(">I", encoded[:4])
        if not 0 < expected <= MAX_COMPRESSED_INLINE_BYTES:
            raise ValueError("Invalid compressed Temporal payload size.")
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(encoded[4:], expected + 1)
        except zlib.error:
            raise ValueError("Invalid compressed Temporal payload.") from None
        if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Invalid compressed Temporal payload.")
        return raw


def encrypted_converter(security: Security, objects: ObjectStore) -> DataConverter:
    return DataConverter(
        payload_codec=EncryptedPayloadCodec(
            security, objects, compress=objects.settings.compress_temporal_payloads,
        ),
        failure_converter_class=DefaultFailureConverterWithEncodedAttributes,
    )

