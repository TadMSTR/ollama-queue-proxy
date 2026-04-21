"""Tests for webhook URL validation (SSRF check)."""

from __future__ import annotations

import socket

import pytest

from ollama_queue_proxy.webhooks import validate_webhook_url


def _fake_getaddrinfo_public(host, port, **kwargs):
    """Return a fake public IP for any hostname."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _fake_getaddrinfo_private(host, port, **kwargs):
    """Return a fake private IP for any hostname."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))]


def test_valid_public_url(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_public)
    validate_webhook_url("https://hooks.example.com/notify")


def test_valid_http_public_url(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_public)
    validate_webhook_url("http://api.example.com/webhook")


def test_empty_url_allowed():
    validate_webhook_url("")


def test_rejects_rfc1918_10x():
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("http://10.0.0.1/webhook")


def test_rejects_rfc1918_192168():
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("http://192.168.1.100/webhook")


def test_rejects_rfc1918_172():
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("http://172.16.0.1/webhook")


def test_rejects_loopback():
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("http://127.0.0.1/webhook")


def test_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="scheme"):
        validate_webhook_url("ftp://example.com/webhook")


def test_rejects_file_scheme():
    with pytest.raises(ValueError, match="scheme"):
        validate_webhook_url("file:///etc/passwd")


def test_rejects_localhost_hostname():
    with pytest.raises(ValueError, match="private|SSRF"):
        validate_webhook_url("http://localhost/webhook")


def test_rejects_link_local_169254():
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("http://169.254.1.1/webhook")
