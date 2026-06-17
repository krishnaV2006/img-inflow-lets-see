"""
Gemini API key pool with round-robin rotation and rate-limit retry.

Configure keys via env vars:
  GEMINI_API_KEY      (always checked — single-key fallback)
  GEMINI_API_KEY_1
  GEMINI_API_KEY_2
  GEMINI_API_KEY_3
  ... up to GEMINI_API_KEY_9 (or however many you add)

Clients are instantiated ONCE at startup and reused across calls so the
underlying HTTP connection is kept alive rather than rebuilt on every request.
On a 429 / resource-exhausted error the next pre-built client is tried
automatically. After exhausting all clients the last exception is raised.
"""

import os
import time
import threading
from google import genai

# ── Build the client pool once at import time ─────────────────────────────────

def _load_keys() -> list[str]:
    keys = []
    for i in range(1, 10):
        k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    base = os.environ.get("GEMINI_API_KEY", "").strip()
    if base and base not in keys:
        keys.append(base)
    return keys or ["DUMMY_KEY"]


_keys  = _load_keys()
_lock  = threading.Lock()
_index = 0

def _build_clients() -> list[genai.Client]:
    return [genai.Client(api_key=k) for k in _keys]

_clients: list[genai.Client] = _build_clients()


def _reset_pool() -> None:
    """Discard all existing connections and rebuild the client pool fresh."""
    global _clients, _index
    with _lock:
        print(f"[GeminiKeys] Resetting connection pool ({len(_keys)} client(s))...")
        _clients = _build_clients()
        _index = 0


# ── Public helpers ────────────────────────────────────────────────────────────

def get_client() -> genai.Client:
    """Return the next pre-built client in the pool (round-robin)."""
    global _index
    with _lock:
        client = _clients[_index % len(_clients)]
        _index = (_index + 1) % len(_clients)
        return client


def call_with_retry(fn, *args, **kwargs):
    """
    Call fn(client) cycling through every key in the pool.
    When all keys are rate-limited: reset the entire connection pool,
    wait 60s, then retry — up to 3 full passes before giving up.
    """
    MAX_PASSES = 2
    last_exc = None
    for pass_num in range(MAX_PASSES):
        for i, client in enumerate(_clients):
            try:
                return fn(client, *args, **kwargs)
            except Exception as e:
                err = str(e).lower()
                if "429" in err or "quota" in err or "resource_exhausted" in err or "rate" in err:
                    print(f"[GeminiKeys] Rate-limited on key …{_keys[i][-6:]}, trying next key.")
                    last_exc = e
                    continue
                raise
        # All keys exhausted — reset pool and wait before next pass
        if pass_num < MAX_PASSES - 1:
            _reset_pool()
            print(f"[GeminiKeys] All {len(_clients)} key(s) exhausted — waiting 5s "
                  f"(pass {pass_num + 1}/{MAX_PASSES})...")
            time.sleep(5)
    raise last_exc


def key_count() -> int:
    return len(_keys)
