from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

app = FastAPI(
    title="VietQR Reader API",
    description="Decode VietQR payloads and QR images into EMV TLV fields.",
    version="1.0.0",
)

BANKS_URL = os.getenv("VIETQR_BANKS_URL", "https://api.vietqr.io/v2/banks")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_CHARS = 8192


class PayloadRequest(BaseModel):
    payload: str = Field(min_length=1, max_length=MAX_PAYLOAD_CHARS)


def _parse_tlv(payload: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    i = 0
    while i < len(payload):
        if i + 4 > len(payload):
            raise ValueError(f"Incomplete TLV header at character {i}")
        tag, length_text = payload[i : i + 2], payload[i + 2 : i + 4]
        if not tag.isdigit() or not length_text.isdigit():
            raise ValueError(f"Invalid TLV header at character {i}")
        length = int(length_text)
        start, end = i + 4, i + 4 + length
        if end > len(payload):
            raise ValueError(f"Field {tag} declares {length} characters beyond payload end")
        fields.append({"tag": tag, "length": length, "value": payload[start:end]})
        i = end
    return fields


def _crc16_ccitt_false(data: bytes) -> str:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def _nested(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for field in fields:
        item = dict(field)
        # EMV merchant-account templates and additional data are themselves TLV strings.
        if field["tag"] in {*(f"{n:02d}" for n in range(26, 52)), "62", "64", "80", "81", "82", "83", "84", "85", "86", "87", "88", "89", "90", "91", "92", "93", "94", "95", "96", "97", "98", "99"}:
            try:
                item["subfields"] = _parse_tlv(field["value"])
            except ValueError:
                item["subfields"] = []
        out.append(item)
    return out


@lru_cache(maxsize=1)
def _bank_catalog() -> dict[str, dict[str, Any]]:
    try:
        with httpx.Client(timeout=4.0, follow_redirects=True) as client:
            response = client.get(BANKS_URL)
            response.raise_for_status()
            payload = response.json()
        return {str(bank.get("bin")): bank for bank in payload.get("data", []) if bank.get("bin")}
    except (httpx.HTTPError, ValueError, TypeError):
        return {}


def _extract(fields: list[dict[str, Any]], raw_payload: str) -> dict[str, Any]:
    by_tag = {field["tag"]: field["value"] for field in fields}
    merchant_account = None
    bank_bin = account = service_code = None
    for field in fields:
        if field["tag"] not in {f"{n:02d}" for n in range(26, 52)}:
            continue
        try:
            sub = {item["tag"]: item["value"] for item in _parse_tlv(field["value"])}
        except ValueError:
            continue
        if sub.get("00") == "A000000727":
            merchant_account = {"tag": field["tag"], "subfields": _nested(_parse_tlv(field["value"]))}
            beneficiary = {}
            # NAPAS account-transfer beneficiary data is commonly nested in subtag 01.
            try:
                beneficiary = {x["tag"]: x["value"] for x in _parse_tlv(sub.get("01", ""))}
            except ValueError:
                beneficiary = {}
            bank_bin, account, service_code = beneficiary.get("00"), beneficiary.get("01"), sub.get("02")
            break

    bank = _bank_catalog().get(bank_bin, {}) if bank_bin else {}
    crc_value = by_tag.get("63")
    crc_valid = False
    if crc_value and raw_payload.endswith("6304" + crc_value):
        crc_valid = _crc16_ccitt_false(raw_payload[:-4].encode("utf-8")) == crc_value.upper()

    name = by_tag.get("59")
    return {
        "format": "VietQR/NAPAS" if merchant_account else "EMVCo-compatible QR",
        "recipient_name": name,
        "recipient_name_source": "tag_59" if name else None,
        "account_number": account,
        "bank_bin": bank_bin,
        "bank": {
            "name": bank.get("shortName") or bank.get("short_name") or bank.get("name"),
            "legal_name": bank.get("name"),
            "code": bank.get("code"),
            "logo": bank.get("logo"),
        } if bank_bin else None,
        "service_code": service_code,
        "amount": by_tag.get("54"),
        "currency": by_tag.get("53"),
        "country": by_tag.get("58"),
        "merchant_city": by_tag.get("60"),
        "crc": {"value": crc_value, "valid": crc_valid} if crc_value else None,
        "fields": _nested(fields),
        "raw_payload": raw_payload,
    }


def _decode(payload: str) -> dict[str, Any]:
    if len(payload) > MAX_PAYLOAD_CHARS:
        raise HTTPException(status_code=413, detail="QR payload is too long")
    try:
        fields = _parse_tlv(payload.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _extract(fields, payload.strip())


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/banks")
def banks() -> list[dict[str, Any]]:
    return list(_bank_catalog().values())


@app.post("/api/v1/parse")
def parse_payload(request: PayloadRequest) -> dict[str, Any]:
    return _decode(request.payload)


@app.post("/api/v1/parse-image")
async def parse_image(image: UploadFile = File(...)) -> dict[str, Any]:
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 8 MB limit")
    if not data:
        raise HTTPException(status_code=400, detail="Image is empty")
    decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise HTTPException(status_code=415, detail="Unsupported or invalid image")
    value, points, _ = cv2.QRCodeDetector().detectAndDecode(decoded)
    if not value:
        raise HTTPException(status_code=422, detail="No readable QR code found")
    return _decode(value)
