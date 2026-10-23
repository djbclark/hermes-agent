"""`/model` validation of a named custom provider whose credential is a ``key_cmd``.

A fake authenticated ``/models`` endpoint (real local HTTP server) proves the minted key reaches the
listing probe, and only the endpoint's own origin ever sees it.
"""

import http.server
import threading

import pytest

from agent.command_token_source import build_command_token_provider
from hermes_cli import models_validate
from hermes_cli.models_validate import validate_requested_model

KEY = "fixture-token"
MODELS = ["us.meta.llama4-scout-17b-instruct-v1:0", "scout-chat"]


class _Server:
    """Serves ``GET <prefix>/models`` only with ``Bearer KEY``; records every Authorization header."""

    def __init__(self, *, prefix="/v1", redirect_to=None):
        self.seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                if redirect_to and self.path == f"{prefix}/models":
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                    self.end_headers()
                    return
                if self.path != f"{prefix}/models":
                    self.send_response(404)
                    self.end_headers()
                    return
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = ('{"data": [' + ",".join('{"id": "%s"}' % m for m in MODELS) + "]}").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def authorizations(self):
        return [auth for _, auth in self.seen]


@pytest.fixture
def server():
    srv = _Server()
    yield srv
    srv.close()


def _validate(model, base_url, key_cmd=f"/usr/bin/printf {KEY}"):
    return validate_requested_model(
        model, "custom:goose-scout", api_key=build_command_token_provider(key_cmd, "goose-scout"),
        base_url=base_url, api_mode="chat_completions")


def test_minted_key_verifies_real_name_and_flags_typo(server):
    ok = _validate("scout-chat", f"{server.base}/v1")
    assert ok["accepted"] and ok["persist"] and not ok.get("message")

    typo = _validate("scout-chatt", f"{server.base}/v1")
    assert typo["accepted"] and typo["persist"]
    assert "not found in this custom endpoint's model listing" in typo["message"]
    assert "scout-chat" in typo["message"]
    # The wire carried the real key, never a stringified callable.
    assert set(server.authorizations) == {f"Bearer {KEY}"}


@pytest.mark.parametrize("key_cmd", ["exit 1", "/usr/bin/printf ''", "kill -9 $$"])
def test_failed_mint_soft_accepts_like_before(server, key_cmd):
    result = _validate("scout-chatt", f"{server.base}/v1", key_cmd=key_cmd)
    assert result["accepted"] and result["persist"]
    assert "could not reach this custom endpoint's model listing" in result["message"]
    assert "not found in this custom endpoint" not in result["message"]
    assert all(auth is None for auth in server.authorizations)


def test_slow_key_cmd_is_bounded_and_soft_accepts(server, monkeypatch):
    monkeypatch.setattr(models_validate, "_PROBE_MINT_TIMEOUT_SECONDS", 0.3)
    result = _validate("scout-chatt", f"{server.base}/v1", key_cmd="sleep 5; /usr/bin/printf fixture-token")
    assert result["accepted"] and "could not reach this custom endpoint's model listing" in result["message"]
    assert all(auth is None for auth in server.authorizations)


def test_cross_origin_redirect_never_receives_key():
    other = _Server(prefix="")
    srv = _Server(redirect_to=f"{other.base}/models")
    try:
        result = _validate("scout-chat", f"{srv.base}/v1")
    finally:
        srv.close()
        other.close()
    assert result["accepted"] and "could not reach this custom endpoint's model listing" in result["message"]
    assert (f"/v1/models", f"Bearer {KEY}") in srv.seen  # own origin got the key
    assert other.seen and all(auth is None for auth in other.authorizations)


def test_alternate_v1_candidate_stays_on_same_origin_and_carries_key(server):
    result = _validate("scout-chatt", server.base)  # configured without /v1; the probe retries ``/v1``
    assert "not found in this custom endpoint's model listing" in result["message"]
    assert "Endpoint verification succeeded" in result["message"]
    assert [auth for path, auth in server.seen if path == "/v1/models"] == [f"Bearer {KEY}"]


def test_string_key_unchanged(server):
    result = validate_requested_model(
        "scout-chatt", "custom:goose-scout", api_key=KEY, base_url=f"{server.base}/v1", api_mode="chat_completions")
    assert "not found in this custom endpoint's model listing" in result["message"]
