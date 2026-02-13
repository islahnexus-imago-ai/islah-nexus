# nexus.py — Islah Nexus Local Wrapper (v0.1 secure spine)
# Goals this file enforces (MVP):
# - Local-only (127.0.0.1)
# - Explicit invocation (POST)
# - Token-gated access (X-NEXUS-TOKEN)
# - Append-only tamper-evident audit (HMAC + hash-chain)
# - Safe error handling + prompt limits
# VA/TK + Receipts come next steps (v0.2+)

from flask import Flask, request, jsonify
import requests
import datetime as dt
import os, json, uuid, time, hashlib, hmac

app = Flask(__name__)

# ---- Config (env-driven) ----
NEXUS_MODEL   = os.getenv("NEXUS_MODEL", "gemma3:4b")
OLLAMA_URL    = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
HOST          = os.getenv("NEXUS_HOST", "127.0.0.1")
PORT          = int(os.getenv("NEXUS_PORT", "9090"))

AUDIT_DIR     = os.getenv("NEXUS_AUDIT_DIR", ".")
AUDIT_FILE    = os.path.join(AUDIT_DIR, "audit_log.jsonl")   # append-only
STATE_FILE    = os.path.join(AUDIT_DIR, "audit_state.json")  # stores last hash

# Secrets must be set (we load them from env; we'll source nexus_secrets.env each session)
NEXUS_TOKEN   = os.getenv("NEXUS_TOKEN", "")
NEXUS_HMAC_KEY= os.getenv("NEXUS_HMAC_KEY", "")

# Limits
MAX_PROMPT_CHARS = int(os.getenv("NEXUS_MAX_PROMPT_CHARS", "8000"))  # strict MVP cap
TIMEOUT_SEC      = int(os.getenv("NEXUS_TIMEOUT_SEC", "120"))

def _now_utc():
    return dt.datetime.utcnow().isoformat() + "Z"

def _hmac_hex(key_hex: str, msg: str) -> str:
    key = bytes.fromhex(key_hex)
    return hmac.new(key, msg.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()

def _sha256_hex(msg: str) -> str:
    return hashlib.sha256(msg.encode("utf-8", errors="replace")).hexdigest()

def _load_last_hash() -> str:
    if not os.path.exists(STATE_FILE):
        return "GENESIS"
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return st.get("last_event_hash", "GENESIS")
    except Exception:
        return "STATE_CORRUPT"

def _save_last_hash(h: str):
    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_event_hash": h, "updated_utc": _now_utc()}, f, indent=2)

def _audit_append(event: dict):
    # Append-only line write (tamper-evident by hash chain + HMAC)
    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def _require_secrets():
    if not NEXUS_TOKEN or not NEXUS_HMAC_KEY:
        return False
    # basic sanity
    if len(NEXUS_TOKEN) < 20:
        return False
    if len(NEXUS_HMAC_KEY) < 32:
        return False
    return True

def _auth_ok(req) -> bool:
    token = req.headers.get("X-NEXUS-TOKEN", "")
    return bool(token) and (token == NEXUS_TOKEN)

def call_ollama(prompt: str) -> str:
    r = requests.post(
        OLLAMA_URL,
        json={"model": NEXUS_MODEL, "prompt": prompt, "stream": False},
        timeout=TIMEOUT_SEC
    )
    r.raise_for_status()
    return r.json().get("response", "")

@app.get("/v1/health")
def health():
    # Actually check ollama reachability (fast + honest)
    ok = True
    err = ""
    try:
        _ = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
    except Exception as e:
        ok = False
        err = str(e)

    return jsonify({
        "status": "OK" if ok else "DEGRADED",
        "mode": "OFFLINE_LOCAL_SECURE_MVP" if ok else "OFFLINE_LOCAL_DEGRADED",
        "model": NEXUS_MODEL,
        "ollama_url": OLLAMA_URL,
        "secrets_loaded": _require_secrets(),
        "ollama_reachable": ok,
        "error": err if not ok else ""
    })

@app.post("/v1/ask")
def ask():
    started = time.time()
    req_id = str(uuid.uuid4())

    # Enforce secrets presence
    if not _require_secrets():
        return jsonify({"request_id": req_id, "error": "Server secrets missing (NEXUS_TOKEN / NEXUS_HMAC_KEY)."}), 500

    # Auth gate
    if not _auth_ok(request):
        return jsonify({"request_id": req_id, "error": "Unauthorized"}), 401

    data = request.json or {}
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"request_id": req_id, "error": "No prompt provided"}), 400

    if len(prompt) > MAX_PROMPT_CHARS:
        return jsonify({"request_id": req_id, "error": f"Prompt too long (max {MAX_PROMPT_CHARS} chars)."}), 413

    # VA/TK placeholders (real enforcement comes next step)
    va_action = "VOID_DEFAULT"
    tk_flags = []

    prev_hash = _load_last_hash()

    try:
        response = call_ollama(prompt)
        latency_ms = int((time.time() - started) * 1000)

        # Privacy-friendly: we record hashes; not raw prompt/response.
        prompt_sha = _sha256_hex(prompt)
        resp_sha   = _sha256_hex(response)

        # Tamper-evident event hash: HMAC over a stable canonical payload
        canonical = json.dumps({
            "timestamp_utc": _now_utc(),
            "request_id": req_id,
            "model": NEXUS_MODEL,
            "route": "/v1/ask",
            "prompt_length": len(prompt),
            "prompt_sha256": prompt_sha,
            "response_length": len(response),
            "response_sha256": resp_sha,
            "latency_ms": latency_ms,
            "va_action": va_action,
            "tk_flags": tk_flags,
            "prev_event_hash": prev_hash
        }, separators=(",", ":"), ensure_ascii=False)

        event_hash = _hmac_hex(NEXUS_HMAC_KEY, canonical)

        event = json.loads(canonical)
        event["event_hash"] = event_hash

        _audit_append(event)
        _save_last_hash(event_hash)

        return jsonify({
            "request_id": req_id,
            "response": response,
            "status": "OFFLINE_LOCAL_SECURE_MVP",
            "mode": "VOID",
            "model": NEXUS_MODEL,
            "va_action": va_action,
            "tk_flags": tk_flags
        })

    except requests.RequestException:
        latency_ms = int((time.time() - started) * 1000)
        # Log minimal error (no internal stack trace to client)
        err_event = {
            "timestamp_utc": _now_utc(),
            "request_id": req_id,
            "route": "/v1/ask",
            "model": NEXUS_MODEL,
            "error": "OLLAMA_REQUEST_FAILED",
            "latency_ms": latency_ms,
            "prev_event_hash": prev_hash
        }
        canonical = json.dumps(err_event, separators=(",", ":"), ensure_ascii=False)
        err_event["event_hash"] = _hmac_hex(NEXUS_HMAC_KEY, canonical)

        _audit_append(err_event)
        _save_last_hash(err_event["event_hash"])

        return jsonify({"request_id": req_id, "error": "Ollama call failed"}), 502

if __name__ == "__main__":
    # Local-only binding is intentional.
    app.run(host=HOST, port=PORT)
