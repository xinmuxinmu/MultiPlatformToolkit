# CodeArts Agent local credentials and gateway protocol

This adapter reads CodeArts Agent's installed VS Code secret store without
starting the editor, extension host, or model kernel. It uses the same Windows
user account that encrypted the local Local State key. The gateway stores its
own credential copy in the application's DPAPI-protected account vault.

The huaweicloud.authentication extension stores HuaweiCloudSession under
secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}
in User/globalStorage/state.vscdb. The extension unwraps Chromium's
os_crypt.encrypted_key with Windows DPAPI and decrypts the v10/v11 record
using AES-GCM. The importer reads the SQLite database read-only. It extracts the
account label and ID, temporary AK/SK/security token, expiration, refresh token,
PKCE verifier, and DPoP key pair. Error messages never include the encrypted
record, tokens, or crypto exception text.

The Huawei STS refresh protocol is POST
https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens with form fields
client_id=codearts-agent, grant_type=refresh_token, code_verifier, and
refresh_token. The request includes an ES256 DPoP proof built from the stored
P-256 key. An expired record is refreshed during import; the caller immediately
saves the returned credential (including a rotated refresh token) into its own
vault. The desktop secret store is not modified.

Huawei signed APIs use SDK-HMAC-SHA256 Authorization, X-Sdk-Date, and
X-Security-Token. The installed extension confirms that the model kernel
calls an OpenAI-compatible /chat/completions endpoint and signs with the same
temporary AK/SK. It also uses app and tracing headers and can negotiate a
separate SM2 encrypted channel for selected requests. Until the exact chat
request envelope, signed headers, and response behavior are verified against
the production model gateway, chat remains disabled in this adapter.

Daily benefit claims visible in the extension are token-benefit claims, not a
verified daily attendance/check-in API. No check-in request is sent. The OAuth
browser callback is bound to 127.0.0.1 on a randomly selected port; a phone
cannot deliver that callback to the user's computer. Therefore standalone QR
login is not advertised. Local credential import is the supported account
acquisition path.

The installed extension's SDK-HMAC-SHA256 signer was inspected, but the
gateway's direct STS identity and model-catalog GETs returned HTTP 401 with
APIGW.0301 after one successful token renewal. The signing path is therefore
not enabled in the adapter, and model listing, chat, and check-in remain
disabled. The renewed credential was saved in the gateway's DPAPI vault; the
installed CodeArts secret store was not changed. Unit tests cover local AES-GCM
decryption, expired-session refresh form/DPoP construction, and credential
rotation. No chat request, check-in claim, app UI, or kernel executable is
started by the adapter.
