// WorkBuddy 5.6+ at-rest credential decryptor.
//
// Invoked only as:
//   ELECTRON_RUN_AS_NODE=1 WorkBuddy.exe wb_helper.js <auth-file> auth.accessToken
// It reads one local field, decrypts it in memory and writes only the plaintext
// field to stdout. It contains no network or UI code.
"use strict";

const fs = require("fs");
const crypto = require("crypto");

function fail(message) {
    throw new Error(message);
}

function u32(value) {
    const out = Buffer.alloc(4);
    out.writeUInt32BE(value >>> 0);
    return out;
}

function lp(value) {
    const data = Buffer.from(value, "utf8");
    return Buffer.concat([u32(data.length), data]);
}

function fieldAad(keyId, suite) {
    return Buffer.concat([
        Buffer.from("WB-AAD\0", "ascii"),
        Buffer.from([1]),
        lp("WBEV1"),
        lp("sym-v1"),
        u32(suite),
        lp(keyId),
        Buffer.from([2, 0, 0]),
    ]);
}

function main() {
    const authPath = process.argv[2];
    const fieldPath = process.argv[3] || "auth.accessToken";
    if (!authPath) fail("missing auth file");

    let document;
    try {
        document = JSON.parse(fs.readFileSync(authPath, "utf8"));
    } catch (_error) {
        fail("cannot read credential file");
    }

    let field = document;
    for (const segment of fieldPath.split(".")) {
        if (!field || typeof field !== "object") fail("credential field not found");
        field = field[segment];
    }
    if (!field || typeof field !== "object" ||
        field.$wbEncrypted !== 1 || typeof field.envelope !== "string") {
        fail("credential field is not an encrypted envelope");
    }

    let binding;
    let payload;
    try {
        if (typeof process._linkedBinding !== "function") {
            fail("WorkBuddy native storage binding unavailable");
        }
        binding = process._linkedBinding("electron_browser_workbuddy_storage");
        payload = JSON.parse(binding.loggerGet());
    } catch (_error) {
        fail("could not obtain local credential key");
    }
    const secret = payload && payload.atRestSecretKey;
    if (typeof secret !== "string" || secret.length !== 44) {
        fail("local credential key is missing or malformed");
    }

    let key;
    let plaintext;
    try {
        key = crypto.createHash("sha256").update(secret, "utf8").digest();
        const envelope = JSON.parse(Buffer.from(field.envelope, "base64").toString("utf8"));
        if (!envelope || envelope.suite !== 1) throw new Error("unsupported envelope");
        const keyId = crypto.createHash("sha256").update(key).digest("hex").slice(0, 16);
        if (envelope.keyId !== keyId) throw new Error("key mismatch");

        const decipher = crypto.createDecipheriv(
            "aes-256-gcm",
            key,
            Buffer.from(envelope.nonce, "base64"),
            { authTagLength: 16 },
        );
        decipher.setAAD(fieldAad(envelope.keyId, envelope.suite));
        decipher.setAuthTag(Buffer.from(envelope.authTag, "base64"));
        plaintext = Buffer.concat([
            decipher.update(Buffer.from(envelope.ciphertext, "base64")),
            decipher.final(),
        ]);
        const result = plaintext.toString("utf8");
        if (!result || result.includes("\0")) throw new Error("invalid plaintext");
        process.stdout.write(result);
    } catch (_error) {
        fail("credential envelope authentication or decryption failed");
    } finally {
        if (key) key.fill(0);
        if (plaintext) plaintext.fill(0);
    }
}

try {
    main();
} catch (error) {
    const message = error && typeof error.message === "string"
        ? error.message
        : "local credential decryption failed";
    process.stderr.write("WB_HELPER_ERROR: " + message + "\n");
    process.exitCode = 1;
}
