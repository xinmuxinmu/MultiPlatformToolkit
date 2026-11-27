import base64
import json
import sqlite3
import time
from cryptography.hazmat.primitives.asymmetric import ec

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from modules.ai_gateway.providers import codearts


def _profile(tmp_path, session, key):
    profile = tmp_path / "codearts-agent"
    storage = profile / "User" / "globalStorage"
    storage.mkdir(parents=True)
    (profile / "Local State").write_text(json.dumps({
        "os_crypt": {"encrypted_key": base64.b64encode(b"DPAPIwrapped").decode()}
    }))
    nonce = b"123456789012"
    encrypted = b"v10" + nonce + AESGCM(key).encrypt(nonce, json.dumps(session).encode(), None)
    with sqlite3.connect(storage / "state.vscdb") as db:
        db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO ItemTable VALUES (?, ?)", (codearts.SECRET_KEY,
                    json.dumps({"type": "Buffer", "data": list(encrypted)})))
    return profile


def test_local_secret_decrypts_without_revealing_it(tmp_path):
    key = b"k" * 32
    session = {"account": {"id": "acct-1", "label": "Demo"}, "accessKey": "AK",
               "secretKey": "SK", "securitytoken": "ST", "expires_at": "2027-01-01T00:00:00Z",
               "refresh_token": "refresh", "loginContext": {"pkcePair": {"codeVerifier": "verifier"}}}
    profile = _profile(tmp_path, session, key)
    result = codearts._read_session(profile, decrypt_key=lambda wrapped: key)
    assert result["account"]["id"] == "acct-1"
    assert result["accessKey"] == "AK"


def test_import_local_renews_expired_secret_and_returns_owned_credential(tmp_path, monkeypatch):
    session = {"account": {"id": "acct-2", "label": "Alice"}, "accessKey": "old-ak",
               "secretKey": "old-sk", "securitytoken": "old-st", "expires_at": "2020-01-01T00:00:00Z",
               "refresh_token": "old-refresh", "domainId": "domain",
               "loginContext": {"pkcePair": {"codeVerifier": "verifier"}}}
    monkeypatch.setattr(codearts, "_read_session", lambda profile: session)
    provider = codearts.CodeArtsProvider(profile_dirs=[tmp_path])
    calls = []

    def refresh(credential):
        calls.append(credential["access_key_id"])
        credential.update(access_key_id="new-ak", secret_access_key="new-sk", security_token="new-st",
                          expires_at=time.time() + 3600, refresh_token="new-refresh")

    monkeypatch.setattr(provider, "_refresh", refresh)
    rows = provider.import_local()
    assert calls == ["old-ak"]
    assert rows[0]["external_id"] == "acct-2"
    assert rows[0]["label"] == "Alice"
    assert rows[0]["credential"]["access_key_id"] == "new-ak"
    assert rows[0]["credential"]["refresh_token"] == "new-refresh"
    assert provider.capabilities["chat"] is False
    assert provider.capabilities["checkin"] is False


def test_refresh_uses_codearts_dpop_and_persists_rotated_fields():
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"credentials": {"access_key_id": "AK2", "secret_access_key": "SK2",
                                     "security_token": "ST2", "expiration": "2027-01-01T00:00:00Z"},
                    "refresh_token": "refresh2"}

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, data, headers):
            assert url == codearts.STS + "/v1/oauth2/tokens"
            assert data["client_id"] == "codearts-agent"
            assert data["grant_type"] == "refresh_token"
            assert headers["DPoP"].count(".") == 2
            return Response()

    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.private_numbers()
    b64 = codearts._b64
    public = {"kty": "EC", "crv": "P-256",
              "x": b64(numbers.public_numbers.x.to_bytes(32, "big")),
              "y": b64(numbers.public_numbers.y.to_bytes(32, "big"))}
    private = {**public, "d": b64(numbers.private_value.to_bytes(32, "big"))}
    context = {"pkcePair": {"codeVerifier": "pkce"},
               "dpopKeyPair": {"privateKeyJwk": private, "publicKeyJwk": public}}
    credential = {"expires_at": 1, "refresh_token": "refresh1", "login_context": context}
    provider = codearts.CodeArtsProvider(client_factory=Client)
    provider._refresh(credential)
    assert credential["access_key_id"] == "AK2"
    assert credential["refresh_token"] == "refresh2"
    assert credential["expires_at"] > time.time()


