"""Remove personal data and secrets from text before it reaches a model.

Applied to everything read from log stores and CI job logs. Regexes reduce the
risk, they do not eliminate it: have the data protection officer validate the
approach against your real log formats before go-live.
"""

from __future__ import annotations

import ipaddress
import re

_SECRETS = [
    re.compile(r"\bglpat-[\w-]{10,}"),
    re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]+"),  # JWT
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
]
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_KEYED_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token)(\s*[=:]\s*)[\"']?[^\s\"',;]+")
_KEYED_ID = re.compile(r"(?i)\b(user[_-]?id|customer[_-]?id|account[_-]?id|client[_-]?id|dni|nif|nie|ssn)(\s*[=:]\s*)[\"']?[\w-]+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_DNI = re.compile(r"\b\d{8}[A-HJ-NP-TV-Z]\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<![\w:])[0-9a-fA-F:]*:[0-9a-fA-F:]+(?![\w:])")
_PHONES = [
    re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?(?:\(?\d{1,4}\)?[\s.-]?){2,5}\d{2,4}\b"),
    re.compile(r"\b\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    re.compile(r"\b[6789]\d{2}[\s.-]?\d{3}[\s.-]?\d{3}\b"),
]


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "<card>" if 13 <= len(digits) <= 19 and _luhn_valid(digits) else match.group(0)


def _mask_ipv4(match: re.Match[str]) -> str:
    try:
        ipaddress.IPv4Address(match.group(0))
    except ValueError:
        return match.group(0)
    return "<ip>"


def _mask_ipv6(match: re.Match[str]) -> str:
    text = match.group(0)
    # Needs a digit and a valid address, so times (17:05:12) and code (add::) survive.
    if "::" not in text and text.count(":") != 7:
        return text
    if not any(ch.isdigit() for ch in text):
        return text
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return text
    return "<ip>"


def mask(text: str) -> str:
    """Return text with secrets, e-mails, IDs, phones, cards and IP addresses replaced."""
    for pattern in _SECRETS:
        text = pattern.sub("<secret>", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} <secret>", text)
    text = _KEYED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<secret>", text)
    text = _KEYED_ID.sub(lambda m: f"{m.group(1)}{m.group(2)}<id>", text)
    text = _EMAIL.sub("<email>", text)
    text = _UUID.sub("<id>", text)
    text = _DNI.sub("<id>", text)
    text = _CARD.sub(_mask_card, text)
    text = _IPV4.sub(_mask_ipv4, text)
    text = _IPV6.sub(_mask_ipv6, text)
    for pattern in _PHONES:
        text = pattern.sub("<phone>", text)
    return text
