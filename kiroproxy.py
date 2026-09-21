#!/usr/bin/env python3
"""
kiroproxy - a small Anthropic-API server that talks to Kiro.

Point Claude Code at it with ANTHROPIC_BASE_URL and your Kiro subscription
answers the requests. Standard library only: no venv, no fastapi, no install.

    python kiroproxy.py              # listens on 127.0.0.1:9100
    python kiroproxy.py --port 9200 --verbose

Credentials come from the kiro-cli sqlite database (the same one `kiro login`
writes). Nothing is copied anywhere; the token is refreshed in place.
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

DEFAULT_DB = os.path.expandvars(
    r"%LOCALAPPDATA%\kiro-cli\data.sqlite3"
) if os.name == "nt" else os.path.expanduser("~/.kiro-cli/data.sqlite3")

DB_PATH = os.environ.get("KIRO_DB", DEFAULT_DB)
TOKEN_KEYS = [
    "kirocli:odic:token",
    "codewhisperer:odic:token",
    "kiro:odic:token",
]
REGISTRATION_KEYS = [
    "kirocli:odic:device-registration",
    "codewhisperer:odic:device-registration",
    "kiro:odic:device-registration",
]

API_HOST_TMPL = "runtime.{region}.kiro.dev"
OIDC_HOST_TMPL = "oidc.{region}.amazonaws.com"
DESKTOP_AUTH_TMPL = "prod.{region}.auth.desktop.kiro.dev"

# Anything Claude Code may ask for -> a model id Kiro accepts.
MODEL_MAP = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4.5",
}
# Probed against the live API on 2026-09-20: opus-5 / sonnet-5 / opus-4.8 all
# answer; there is no 1M-context variant id - claude-opus-5[1m] and friends
# come back INVALID_MODEL_ID. haiku-5 does not exist on Kiro either.
KNOWN_MODELS = [
    "auto",
    "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4.8", "claude-opus-4.7", "claude-opus-4.6", "claude-opus-4.5",
    "claude-sonnet-4.6", "claude-sonnet-4.5", "claude-sonnet-4",
    "claude-haiku-4.5",
    "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
    "glm-5", "minimax-m2.5", "minimax-m2.1", "deepseek-3.2", "qwen3-coder-next",
]
DEFAULT_MODEL = os.environ.get("KIRO_DEFAULT_MODEL", "claude-opus-5")
SMALL_MODEL = os.environ.get("KIRO_SMALL_MODEL", "claude-haiku-4.5")

# Thinking. Kiro accepts additionalModelRequestFields.thinking.type in
# ("adaptive", "disabled") and output_config.effort in
# ("low", "medium", "high", "xhigh", "max"). Probed live 2026-09-21.
THINKING_DEFAULT = os.environ.get("KIRO_THINKING", "adaptive")
EFFORT_DEFAULT = os.environ.get("KIRO_EFFORT", "high")
VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")

MAX_TOOL_NAME = 64
# Kiro's limit is a token count; this byte cap only approximates it, and the
# approximation is poor. Probed live 2026-09-21 on claude-opus-5: real source
# code ran 1.58MB at 54% context, English prose 3.8MB at 92%, but a file padded
# with long runs of a single character hit 65% in just 691KB. So the cap is set
# high enough that Claude Code's own compaction - which leaves a summary behind
# - is what normally fires, and CONTENT_LENGTH_EXCEEDS_THRESHOLD is caught and
# retried for the cases bytes fail to predict. Claude Code compacts around 80%
# of its window; on a 1M model at typical code density that is roughly 2.5MB,
# so the cap must sit above that or the proxy would always trim first.
MAX_PAYLOAD_BYTES = int(os.environ.get("KIRO_MAX_PAYLOAD_BYTES", "2700000"))
REQUEST_TIMEOUT = int(os.environ.get("KIRO_TIMEOUT", "600"))

VERBOSE = False


LOG_FILE = os.environ.get(
    "KIRO_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.log"))
_log_fh = None


def _log_target():
    """Append to proxy.log regardless of how the process was launched.

    The Windows launcher uses a hidden Start-Process with no redirection, so
    anything written only to stderr is lost - including the trim warnings.
    """
    global _log_fh
    if _log_fh is None and LOG_FILE:
        try:
            # If stderr is already pointed at this same file (the PowerShell
            # launcher does that with -RedirectStandardError), writing it a
            # second time here would duplicate every line.
            if os.path.exists(LOG_FILE):
                try:
                    a = os.fstat(sys.stderr.fileno())
                    b = os.stat(LOG_FILE)
                    if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                        _log_fh = False
                        return None
                except Exception:
                    pass
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 4 << 20:
                prev = os.path.splitext(LOG_FILE)[0] + ".prev.log"
                os.replace(LOG_FILE, prev)
            _log_fh = open(LOG_FILE, "a", encoding="utf-8")
        except Exception:
            _log_fh = False        # tried once, do not keep retrying
    return _log_fh or None


def log(*a):
    line = " ".join([time.strftime("%H:%M:%S")] + [str(x) for x in a])
    print(line, file=sys.stderr, flush=True)
    fh = _log_target()
    if fh:
        try:
            fh.write(line + "\n")
            fh.flush()
        except Exception:
            pass


def vlog(*a):
    if VERBOSE:
        log(*a)


# ----------------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------------

class Credentials:
    """Reads the kiro-cli token out of sqlite and refreshes it when stale.

    The read path never takes a lock: a request that finds a valid token in
    memory goes straight out. Only an actual refresh serializes, and a second
    caller re-checks after acquiring the lock instead of refreshing again.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._token = None
        self._refresh = None
        self._expires = 0.0
        self._region = "us-east-1"
        self._client_id = None
        self._client_secret = None
        self._profile_arn = None
        self._key = None
        self.load()

    # -- sqlite ---------------------------------------------------------
    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def load(self):
        if not os.path.exists(self.db_path):
            raise SystemExit("kiro database not found: %s\nRun `kiro login` first, "
                             "or set KIRO_DB." % self.db_path)
        conn = self._connect()
        try:
            row = None
            for key in TOKEN_KEYS:
                row = conn.execute(
                    "SELECT value FROM auth_kv WHERE key = ?", (key,)).fetchone()
                if row:
                    self._key = key
                    break
            if not row:
                raise SystemExit("no kiro token in %s - run `kiro login`" % self.db_path)
            data = json.loads(row[0])
            self._token = data.get("access_token")
            self._refresh = data.get("refresh_token")
            self._region = data.get("region") or self._region
            self._expires = _parse_expiry(data.get("expires_at"))

            for key in REGISTRATION_KEYS:
                r = conn.execute(
                    "SELECT value FROM auth_kv WHERE key = ?", (key,)).fetchone()
                if r:
                    reg = json.loads(r[0])
                    self._client_id = reg.get("client_id")
                    self._client_secret = reg.get("client_secret")
                    self._region = reg.get("region") or self._region
                    break

            p = conn.execute(
                "SELECT value FROM state WHERE key = 'api.codewhisperer.profile'").fetchone()
            if p:
                try:
                    self._profile_arn = json.loads(p[0]).get("arn")
                except Exception:
                    pass
        finally:
            conn.close()
        vlog("credentials loaded: region=%s profile=%s expires_in=%ds"
             % (self._region, bool(self._profile_arn), self._expires - time.time()))

    def _save(self):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM auth_kv WHERE key = ?", (self._key,)).fetchone()
            data = json.loads(row[0]) if row else {}
            data["access_token"] = self._token
            data["refresh_token"] = self._refresh
            data["expires_at"] = datetime.fromtimestamp(
                self._expires, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            conn.execute("UPDATE auth_kv SET value = ? WHERE key = ?",
                         (json.dumps(data), self._key))
            conn.commit()
        except Exception as e:      # a locked db must not fail the request
            log("warning: could not write token back to sqlite: %s" % e)
        finally:
            conn.close()

    # -- public ---------------------------------------------------------
    @property
    def region(self):
        return self._region

    @property
    def profile_arn(self):
        return self._profile_arn

    def token(self):
        if self._token and time.time() < self._expires - 300:
            return self._token
        with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            # kiro-cli may have refreshed underneath us; cheapest fix first.
            self.load()
            if self._token and time.time() < self._expires - 300:
                return self._token
            self._do_refresh()
            return self._token

    def _do_refresh(self):
        if not self._refresh:
            raise RuntimeError("no refresh token - run `kiro login`")
        if self._client_id and self._client_secret:
            host = OIDC_HOST_TMPL.format(region=self._region)
            path = "/token"
            body = {
                "clientId": self._client_id,
                "clientSecret": self._client_secret,
                "grantType": "refresh_token",
                "refreshToken": self._refresh,
            }
        else:
            host = DESKTOP_AUTH_TMPL.format(region=self._region)
            path = "/refreshToken"
            body = {"refreshToken": self._refresh}

        log("refreshing kiro token via %s" % host)
        conn = http.client.HTTPSConnection(host, timeout=30)
        try:
            conn.request("POST", path, json.dumps(body),
                         {"Content-Type": "application/json",
                          "User-Agent": "kiroproxy/1.0"})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError("token refresh failed (%s): %s"
                                   % (resp.status, raw[:400].decode("utf-8", "replace")))
            data = json.loads(raw)
        finally:
            conn.close()

        self._token = data.get("accessToken") or data.get("access_token")
        if not self._token:
            raise RuntimeError("refresh response had no accessToken: %r" % data)
        self._refresh = (data.get("refreshToken") or data.get("refresh_token")
                         or self._refresh)
        self._expires = time.time() + int(data.get("expiresIn", 3600))
        if data.get("profileArn"):
            self._profile_arn = data["profileArn"]
        self._save()
        log("token refreshed, valid for %d min" % ((self._expires - time.time()) / 60))


def _parse_expiry(value):
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


# ----------------------------------------------------------------------------
# Anthropic request -> Kiro payload
# ----------------------------------------------------------------------------

def resolve_model(name):
    if not name:
        return DEFAULT_MODEL
    n = str(name).strip()
    if n in KNOWN_MODELS:
        return n
    low = n.lower()
    if low in MODEL_MAP:
        return MODEL_MAP[low]
    # claude-sonnet-4-5-20250929 -> claude-sonnet-4.5
    m = re.match(r"^(?:.*\.)?claude-(opus|sonnet|haiku)-(\d+)(?:[-.](\d+))?", low)
    if m:
        family, major, minor = m.group(1), m.group(2), m.group(3)
        candidate = "claude-%s-%s" % (family, major)
        if minor:
            candidate += "." + minor
        if candidate in KNOWN_MODELS:
            return candidate
        for known in KNOWN_MODELS:
            if known.startswith("claude-%s-" % family):
                return known
    if "haiku" in low:
        return SMALL_MODEL
    return DEFAULT_MODEL


class ToolNames:
    """Kiro caps tool names at 64 chars; MCP names blow past that."""

    def __init__(self):
        self.out = {}   # real -> short
        self.back = {}  # short -> real

    def shorten(self, name):
        if not name:
            return "tool"
        if len(name) <= MAX_TOOL_NAME:
            self.back.setdefault(name, name)
            return name
        if name in self.out:
            return self.out[name]
        digest = hashlib.sha1(name.encode()).hexdigest()[:8]
        short = name[:MAX_TOOL_NAME - 9] + "_" + digest
        self.out[name] = short
        self.back[short] = name
        return short

    def restore(self, name):
        return self.back.get(name, name)


def sanitize_schema(schema):
    """Kiro 400s on empty `required` arrays and on additionalProperties."""
    if not isinstance(schema, dict):
        return {}
    out = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "required" and isinstance(value, list) and not value:
            continue
        if key in ("$schema", "$id"):
            continue
        if isinstance(value, dict):
            out[key] = sanitize_schema(value)
        elif isinstance(value, list):
            out[key] = [sanitize_schema(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def block_text(block, structured=False):
    """Turn a content block into text.

    `structured` means tool_use/tool_result are carried separately in the Kiro
    payload, so they must not be duplicated into the text - otherwise every
    later turn would resend the whole tool call and its output twice.
    """
    if block is None:
        return ""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return str(block)
    btype = block.get("type")
    if btype == "text":
        return block.get("text", "")
    if btype in ("thinking", "redacted_thinking"):
        return ""
    if btype == "image":
        return "(image)"
    if btype in ("tool_use", "tool_result"):
        if structured:
            return ""
        if btype == "tool_use":
            return "[tool: %s] %s" % (block.get("name", "?"),
                                      json.dumps(block.get("input") or {}))
        return "[tool result] " + content_to_text(block.get("content"))
    if "text" in block and isinstance(block["text"], str):
        return block["text"]
    return json.dumps(block, ensure_ascii=False)


def content_to_text(content, structured=False):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block_text(b, structured) for b in content)
    return block_text(content, structured)


def extract_images(content):
    """Top-level image blocks plus images nested inside tool_result content."""
    images = []
    if not isinstance(content, list):
        return images
    blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                blocks.extend(b for b in inner if isinstance(b, dict))
            continue
        blocks.append(block)
    for block in blocks:
        if block.get("type") != "image":
            continue
        source = block.get("source") or {}
        data = source.get("data", "")
        media = source.get("media_type", "image/jpeg")
        if isinstance(data, str) and data.startswith("data:"):
            head, _, tail = data.partition(",")
            data = tail
            if ";" in head and "/" in head:
                media = head[5:head.index(";")]
        fmt = media.split("/")[-1].lower()
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in ("png", "jpeg", "gif", "webp"):
            continue
        if data:
            images.append({"format": fmt, "source": {"bytes": data}})
    return images


def extract_reasoning(content):
    """Pull a signed thinking block out of an assistant turn.

    Claude Code hands back the thinking blocks it was given. Kiro accepts them
    on assistantResponseMessage.reasoningContent as a single object - a list
    there is a 400. Unsigned blocks are dropped: without the signature the
    round-trip is worthless and Kiro keeps continuity itself anyway.
    """
    if not isinstance(content, list):
        return None
    text, signature = [], None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "thinking":
            continue
        text.append(block.get("thinking") or "")
        signature = block.get("signature") or signature
    if not signature:
        return None
    return {"reasoningText": {"text": "".join(text), "signature": signature}}


# Which models accept additionalModelRequestFields, and in which dialect.
# Probed live 2026-09-21: claude 4.6+/5 take thinking + output_config.effort;
# gpt-5.6 takes reasoning.effort; claude 4.5 and below (including the haiku
# small-model Claude Code uses for titles) reject the field outright with
# "additionalModelRequestFields is not supported for this model".
def effort_dialect(model_id):
    m = (model_id or "").lower()
    if m.startswith("gpt-"):
        return "reasoning"
    if m.startswith("claude-"):
        try:
            version = float(m.split("-")[2])
        except (IndexError, ValueError):
            return None
        return "output_config" if version >= 4.6 else None
    return None


def model_request_fields(body, model_id):
    """additionalModelRequestFields from what Claude Code asked for.

    Claude Code sends `thinking` as {"type": "enabled", "budget_tokens": N}.
    Kiro has no budget knob - it takes "adaptive" or "disabled" plus a coarse
    effort level - so the budget is mapped onto an effort instead.
    """
    dialect = effort_dialect(model_id)
    if THINKING_DEFAULT == "off" or not dialect:
        return None

    asked = body.get("thinking")
    mode = THINKING_DEFAULT
    effort = EFFORT_DEFAULT
    if isinstance(asked, dict):
        atype = asked.get("type")
        if atype == "disabled":
            mode = "disabled"
        elif atype in ("enabled", "adaptive"):
            mode = "adaptive"
        budget = asked.get("budget_tokens")
        if isinstance(budget, int):
            # Claude Code's budget -> Kiro's nearest effort step.
            for limit, level in ((4000, "low"), (10000, "medium"),
                                 (24000, "high"), (48000, "xhigh")):
                if budget <= limit:
                    effort = level
                    break
            else:
                effort = "max"

    cfg = body.get("output_config")
    if isinstance(cfg, dict) and cfg.get("effort") in VALID_EFFORT:
        effort = cfg["effort"]

    if dialect == "reasoning":          # gpt-5.6 has no thinking toggle
        return {"reasoning": {"effort": effort}} if effort in VALID_EFFORT else None

    fields = {"thinking": {"type": mode}}
    if effort in VALID_EFFORT:
        fields["output_config"] = {"effort": effort}
    return fields


def extract_tool_uses(content, names):
    uses = []
    if not isinstance(content, list):
        return uses
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            uses.append({
                "name": names.shorten(block.get("name", "")),
                "input": block.get("input") or {},
                "toolUseId": block.get("id") or str(uuid.uuid4()),
            })
    return uses


def extract_tool_results(content):
    results = []
    if not isinstance(content, list):
        return results
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        text = content_to_text(block.get("content")) or "(empty result)"
        results.append({
            "content": [{"text": text}],
            "status": "error" if block.get("is_error") else "success",
            "toolUseId": block.get("tool_use_id") or "",
        })
    return results


def normalize_messages(messages):
    """Kiro wants strictly alternating user/assistant, starting with user."""
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        role = "assistant" if role == "assistant" else "user"
        if out and out[-1]["role"] == role:
            prev = out[-1]
            a = prev["content"] if isinstance(prev["content"], list) else [
                {"type": "text", "text": str(prev["content"])}]
            b = msg.get("content")
            b = b if isinstance(b, list) else [{"type": "text", "text": str(b or "")}]
            prev["content"] = a + b
            continue
        out.append({"role": role, "content": msg.get("content")})

    while out and out[0]["role"] == "assistant":
        out.pop(0)
    return out


def build_payload(body, names, conversation_id):
    model_id = resolve_model(body.get("model"))

    system_prompt = content_to_text(body.get("system"))
    structured = bool(body.get("tools"))
    messages = normalize_messages(body.get("messages"))
    if not messages:
        messages = [{"role": "user", "content": "(empty)"}]

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        raw_name = tool.get("name")
        if not raw_name:
            continue  # server-side tools without a name mean nothing to Kiro
        schema = sanitize_schema(tool.get("input_schema") or tool.get("parameters"))
        if not schema:
            schema = {"type": "object", "properties": {}}
        desc = (tool.get("description") or "").strip() or ("Tool: " + raw_name)
        tools.append({"toolSpecification": {
            "name": names.shorten(raw_name),
            "description": desc[:10000],
            "inputSchema": {"json": schema},
        }})

    history = []
    for msg in messages[:-1]:
        content = msg["content"]
        text = content_to_text(content, structured)
        if msg["role"] == "user":
            results = extract_tool_results(content) if tools else []
            # A turn that only carries tool results must keep an EMPTY content:
            # any placeholder text there makes the model answer the placeholder
            # instead of the tool output.
            entry = {"content": text or ("" if results else "(empty)"),
                     "modelId": model_id, "origin": "AI_EDITOR"}
            if results:
                entry["userInputMessageContext"] = {"toolResults": results}
            history.append({"userInputMessage": entry})
        else:
            entry = {"content": text}
            uses = extract_tool_uses(content, names) if tools else []
            if uses:
                entry["toolUses"] = uses
            reasoning = extract_reasoning(content)
            if reasoning:
                entry["reasoningContent"] = reasoning
            history.append({"assistantResponseMessage": entry})

    # Kiro history must be user/assistant pairs; a dangling user entry breaks it.
    if len(history) % 2 == 1:
        history.append({"assistantResponseMessage": {"content": "(continuing)"}})

    current = messages[-1]
    current_text = content_to_text(current["content"], structured)
    if current["role"] == "assistant":
        history.append({"assistantResponseMessage": {"content": current_text}})
        if len(history) % 2 == 1:
            history.insert(len(history) - 1,
                           {"userInputMessage": {"content": "(continuing)",
                                                 "modelId": model_id,
                                                 "origin": "AI_EDITOR"}})
        current_text = "Continue."

    context = {}
    current_results = []
    if tools:
        context["tools"] = tools
        current_results = extract_tool_results(current["content"])
        if current_results:
            context["toolResults"] = current_results

    user_message = {
        "content": current_text or ("" if current_results else "(empty)"),
        "modelId": model_id,
        "origin": "AI_EDITOR",
    }
    images = extract_images(current["content"])
    if images:
        user_message["images"] = images
    if context:
        user_message["userInputMessageContext"] = context

    fields = model_request_fields(body, model_id)

    payload = {"conversationState": {
        "chatTriggerType": "MANUAL",
        "conversationId": conversation_id,
        "currentMessage": {"userInputMessage": user_message},
    }}
    if history:
        payload["conversationState"]["history"] = history
    if fields:
        payload["additionalModelRequestFields"] = fields

    # The system prompt is injected only AFTER trimming. Kiro's API has no
    # systemPrompt field (probed live: it 400s in every shape), so the prompt
    # has to ride on the oldest user message - and trim_payload eats history
    # from the front. Doing this before the trim silently deleted Claude Code's
    # whole system prompt on every request over the size cap, which left Kiro's
    # own "I am Kiro, built by AWS" persona answering. Reserve keeps it whole.
    fit_payload(payload, system_prompt)
    return payload, model_id, system_prompt


def fit_payload(payload, system_prompt, budget=None):
    """Trim to fit, drop orphaned tool results, then add the system prompt.

    Order matters. Kiro has no systemPrompt field (probed: it 400s in every
    shape), so the prompt has to ride on the oldest user message - and the trim
    deletes history from the front. Injecting before the trim silently deleted
    Claude Code's whole system prompt on any request over the cap, which left
    Kiro's own "I am Kiro, built by AWS" persona answering. Reserving its bytes
    keeps a later injection from pushing the request back over.

    Callable again on the same payload after a rejection, to trim harder.
    """
    reserve = len(system_prompt.encode()) + 64 if system_prompt else 0
    trimmed = trim_payload(payload, reserve, budget)
    prune_orphan_tool_results(payload)

    if system_prompt:
        state = payload["conversationState"]
        hist = state.get("history") or []
        if hist and "userInputMessage" in hist[0]:
            target = hist[0]["userInputMessage"]
        else:
            target = state["currentMessage"]["userInputMessage"]
        if not target.get("content", "").startswith(system_prompt):
            target["content"] = system_prompt + "\n\n" + target["content"]
    return trimmed


def trim_payload(payload, reserve=0, budget=None):
    """Drop the oldest history pairs until the request fits Kiro's limit.

    `reserve` holds back room for bytes added after this runs (the system
    prompt), so injecting it later cannot push the request back over the cap.
    """
    state = payload["conversationState"]
    budget = (MAX_PAYLOAD_BYTES if budget is None else budget) - reserve
    before = len(state.get("history", []))
    while len(json.dumps(state).encode()) > budget:
        history = state.get("history")
        if not history or len(history) < 2:
            break
        del history[0:2]
        if not history:
            state.pop("history", None)
    after = len(state.get("history", []))
    if after < before:
        log("TRIMMED %d of %d history entries to fit %d bytes - the oldest "
            "turns are gone from this request" % (before - after, before, budget))
    vlog("payload %d bytes, %d history entries"
         % (len(json.dumps(state).encode()), after))
    return before - after


def _tool_use_ids(entry):
    """toolUseIds offered by an assistant history entry."""
    if not isinstance(entry, dict):
        return set()
    msg = entry.get("assistantResponseMessage")
    if not isinstance(msg, dict):
        return set()
    return {u.get("toolUseId") for u in msg.get("toolUses") or []
            if isinstance(u, dict) and u.get("toolUseId")}


def prune_orphan_tool_results(payload):
    """Drop toolResults whose toolUse is no longer in the preceding message.

    trim_payload() deletes the oldest history entries in pairs. History
    alternates user/assistant, so a cut can remove the assistant entry that
    issued a toolUse while keeping the user entry that answers it. Kiro then
    rejects the whole request with TOOL_USE_RESULT_MISMATCH, and because the
    trim is deterministic every retry reproduces it. Kiro scopes the match to
    the immediately previous message, so that is the scope used here.
    """
    state = payload.get("conversationState") or {}
    history = state.get("history") or []
    dropped = 0

    for i, entry in enumerate(history):
        msg = entry.get("userInputMessage") if isinstance(entry, dict) else None
        if not isinstance(msg, dict):
            continue
        context = msg.get("userInputMessageContext")
        if not isinstance(context, dict) or not context.get("toolResults"):
            continue
        allowed = _tool_use_ids(history[i - 1]) if i else set()
        kept = [r for r in context["toolResults"]
                if isinstance(r, dict) and r.get("toolUseId") in allowed]
        dropped += len(context["toolResults"]) - len(kept)
        if kept:
            context["toolResults"] = kept
            continue
        context.pop("toolResults", None)
        if not context:
            msg.pop("userInputMessageContext", None)
        # An empty content is only safe while real tool output is attached.
        if not msg.get("content"):
            msg["content"] = "(earlier tool output trimmed)"

    current = (state.get("currentMessage") or {}).get("userInputMessage")
    if isinstance(current, dict):
        context = current.get("userInputMessageContext")
        if isinstance(context, dict) and context.get("toolResults"):
            allowed = _tool_use_ids(history[-1]) if history else set()
            kept = [r for r in context["toolResults"]
                    if isinstance(r, dict) and r.get("toolUseId") in allowed]
            dropped += len(context["toolResults"]) - len(kept)
            if kept:
                context["toolResults"] = kept
            else:
                context.pop("toolResults", None)
                if not current.get("content"):
                    current["content"] = "(earlier tool output trimmed)"

    if dropped:
        log("dropped %d orphaned tool result(s) after trim" % dropped)
    return dropped


# ----------------------------------------------------------------------------
# AWS event stream decoding
# ----------------------------------------------------------------------------

class EventStreamDecoder:
    """Decodes vnd.amazon.eventstream frames into (event_type, payload) pairs.

    Falls back to scanning the buffer for JSON objects if the framing ever
    looks wrong, so a protocol tweak degrades instead of hanging.
    """

    def __init__(self):
        self.buf = b""

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while len(self.buf) >= 16:
            total, headers_len = struct.unpack(">II", self.buf[:8])
            if total < 16 or total > 64 * 1024 * 1024:
                out.extend(self._salvage())
                break
            if len(self.buf) < total:
                break
            frame, self.buf = self.buf[:total], self.buf[total:]
            try:
                out.append(self._parse_frame(frame, headers_len))
            except Exception as e:
                vlog("frame parse error: %s" % e)
        return [x for x in out if x]

    def _parse_frame(self, frame, headers_len):
        headers = {}
        pos = 12
        end = 12 + headers_len
        while pos < end:
            name_len = frame[pos]
            pos += 1
            name = frame[pos:pos + name_len].decode("utf-8", "replace")
            pos += name_len
            vtype = frame[pos]
            pos += 1
            if vtype == 7:      # string
                (vlen,) = struct.unpack(">H", frame[pos:pos + 2])
                pos += 2
                value = frame[pos:pos + vlen].decode("utf-8", "replace")
                pos += vlen
            elif vtype in (0, 1):
                value = vtype == 0
            elif vtype == 2:
                value = frame[pos]; pos += 1
            elif vtype == 3:
                value = struct.unpack(">h", frame[pos:pos + 2])[0]; pos += 2
            elif vtype == 4:
                value = struct.unpack(">i", frame[pos:pos + 4])[0]; pos += 4
            elif vtype == 5:
                value = struct.unpack(">q", frame[pos:pos + 8])[0]; pos += 8
            elif vtype == 6:
                (vlen,) = struct.unpack(">H", frame[pos:pos + 2])
                pos += 2
                value = frame[pos:pos + vlen]; pos += vlen
            elif vtype == 8:
                value = struct.unpack(">q", frame[pos:pos + 8])[0]; pos += 8
            elif vtype == 9:
                value = frame[pos:pos + 16]; pos += 16
            else:
                raise ValueError("unknown header type %d" % vtype)
            headers[name] = value

        body = frame[end:-4]
        try:
            data = json.loads(body.decode("utf-8", "replace")) if body else {}
        except Exception:
            data = {"raw": body.decode("utf-8", "replace")}
        kind = headers.get(":event-type") or headers.get(":exception-type") or ""
        if headers.get(":message-type") == "exception" or ":exception-type" in headers:
            return ("exception", {"type": kind, "data": data})
        return (kind, data)

    def _salvage(self):
        """Framing lost; pull whatever JSON objects are left out of the buffer."""
        text = self.buf.decode("utf-8", "replace")
        self.buf = b""
        events = []
        for match in re.finditer(r'\{"(content|name|input|stop|toolUseId)"', text):
            start = match.start()
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if esc:
                    esc = False
                    continue
                if ch == "\\" and in_str:
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start:i + 1])
                            if "content" in data:
                                kind = "assistantResponseEvent"
                            elif "signature" in data or (
                                    "text" in data and "toolUseId" not in data):
                                kind = "reasoningContentEvent"
                            else:
                                kind = "toolUseEvent"
                            events.append((kind, data))
                        except Exception:
                            pass
                        break
        return events


# ----------------------------------------------------------------------------
# Kiro call
# ----------------------------------------------------------------------------

def kiro_headers(token):
    fingerprint = hashlib.sha256(
        ("%s-%s-kiroproxy" % (socket.gethostname(), os.environ.get("USERNAME", "u"))
         ).encode()).hexdigest()
    agent = ("aws-sdk-js/1.0.27 ua/2.1 os/win32#10.0.19044 lang/js md/nodejs#22.21.1 "
             "api/codewhispererstreaming#1.0.27 m/E KiroIDE-0.7.45-%s" % fingerprint)
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/x-amz-json-1.0",
        "x-amz-target": "AmazonCodeWhispererStreamingService.GenerateAssistantResponse",
        "User-Agent": agent,
        "x-amz-user-agent": "aws-sdk-js/1.0.27 KiroIDE-0.7.45-%s" % fingerprint,
        "x-amzn-codewhisperer-optout": "true",
        "x-amzn-kiro-agent-mode": "vibe",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
    }


# conversation id -> last contextUsagePercentage Kiro reported. Byte size is a
# terrible predictor of Kiro's token limit: real source code ran 1.58MB at 54%
# context, but a file padded with long runs of one character hit 65% in 691KB.
# Feeding the previous turn's real usage back in beats guessing from bytes.
CONTEXT_SEEN = {}


def budget_for(convo):
    """Byte budget for this conversation, tightened by what Kiro last reported."""
    pct = CONTEXT_SEEN.get(convo)
    if not pct or pct < 80:
        return None                      # plenty of room, use the default cap
    # Aim to land near 80% of the window at the density we actually observed.
    return max(200000, min(MAX_PAYLOAD_BYTES, int(MAX_PAYLOAD_BYTES * (80.0 / pct))))


def remember_context(convo, pct):
    """Keep the last reading per conversation, bounded so it cannot grow forever."""
    CONTEXT_SEEN[convo] = pct
    while len(CONTEXT_SEEN) > 200:
        CONTEXT_SEEN.pop(next(iter(CONTEXT_SEEN)))


def call_kiro(creds, payload):
    token = creds.token()
    if creds.profile_arn:
        payload = dict(payload, profileArn=creds.profile_arn)
    host = API_HOST_TMPL.format(region=creds.region)
    body = json.dumps(payload).encode()

    for attempt in (1, 2):
        conn = http.client.HTTPSConnection(host, timeout=REQUEST_TIMEOUT)
        conn.request("POST", "/generateAssistantResponse", body, kiro_headers(token))
        resp = conn.getresponse()
        if resp.status == 200:
            return conn, resp
        detail = resp.read()[:2000].decode("utf-8", "replace")
        conn.close()
        if (resp.status == 400 and attempt == 1
                and "additionalModelRequestFields" in detail
                and "additionalModelRequestFields" in payload):
            # A model Kiro does not offer thinking on. Drop it and go again
            # rather than failing the turn.
            log("model rejected additionalModelRequestFields, retrying without it")
            payload = {k: v for k, v in payload.items()
                       if k != "additionalModelRequestFields"}
            body = json.dumps(payload).encode()
            continue
        if resp.status in (401, 403) and attempt == 1:
            log("auth rejected (%s), forcing refresh" % resp.status)
            with creds._lock:
                creds._do_refresh()
            token = creds.token()
            continue
        raise KiroError(resp.status, detail)


class KiroError(Exception):
    def __init__(self, status, detail):
        super().__init__("kiro %s: %s" % (status, detail))
        self.status = status
        self.detail = detail


# ----------------------------------------------------------------------------
# Kiro events -> Anthropic events
# ----------------------------------------------------------------------------

class ResponseBuilder:
    """Consumes Kiro events and emits Anthropic SSE lines in order."""

    def __init__(self, model, message_id, names):
        self.model = model
        self.id = message_id
        self.names = names
        self.index = -1
        self.open_kind = None      # "text", "tool" or "thinking"
        self.tool = None
        self.think_buf = []
        self.think_sig = None
        self.context_pct = None
        self.blocks = []           # accumulated, for the non-streaming reply
        self.text_buf = []
        self.stop_reason = "end_turn"
        self.last_text = None

    # -- block plumbing -------------------------------------------------
    def _close(self):
        if self.open_kind is None:
            return []
        out = [sse("content_block_stop", {"type": "content_block_stop",
                                          "index": self.index})]
        if self.open_kind == "text":
            self.blocks.append({"type": "text", "text": "".join(self.text_buf)})
            self.text_buf = []
        elif self.open_kind == "thinking":
            thought = "".join(self.think_buf)
            # A thinking block with no text is noise; Kiro sometimes sends a
            # signature with nothing in front of it.
            if thought:
                self.blocks.append({"type": "thinking", "thinking": thought,
                                    "signature": self.think_sig or ""})
            self.think_buf = []
            self.think_sig = None
        else:
            raw = self.tool["json"]
            try:
                args = json.loads(raw) if raw.strip() else {}
            except Exception:
                args = {"_raw": raw}
            self.blocks.append({"type": "tool_use", "id": self.tool["id"],
                                "name": self.tool["name"], "input": args})
            if self.stop_reason != "refusal":
                self.stop_reason = "tool_use"
            self.tool = None
        self.open_kind = None
        return out

    def _open_text(self):
        if self.open_kind == "text":
            return []
        out = self._close()
        self.index += 1
        self.open_kind = "text"
        out.append(sse("content_block_start", {
            "type": "content_block_start", "index": self.index,
            "content_block": {"type": "text", "text": ""}}))
        return out

    def _open_thinking(self):
        if self.open_kind == "thinking":
            return []
        out = self._close()
        self.index += 1
        self.open_kind = "thinking"
        out.append(sse("content_block_start", {
            "type": "content_block_start", "index": self.index,
            "content_block": {"type": "thinking", "thinking": "",
                              "signature": ""}}))
        return out

    # -- events ---------------------------------------------------------
    def handle(self, kind, data):
        if kind in ("assistantResponseEvent", "") and "content" in data:
            text = data.get("content") or ""
            if not text or data.get("followupPrompt"):
                return []
            if text == self.last_text:   # Kiro sometimes repeats a chunk verbatim
                return []
            self.last_text = text
            out = self._open_text()
            self.text_buf.append(text)
            out.append(sse("content_block_delta", {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "text_delta", "text": text}}))
            return out

        if kind == "reasoningContentEvent":
            # Kiro streams thinking as text deltas then one signature. The
            # signature is what lets the block be replayed on the next turn.
            out = []
            text = data.get("text")
            if text:
                out.extend(self._open_thinking())
                self.think_buf.append(text)
                out.append(sse("content_block_delta", {
                    "type": "content_block_delta", "index": self.index,
                    "delta": {"type": "thinking_delta", "thinking": text}}))
            sig = data.get("signature")
            if sig:
                out.extend(self._open_thinking())
                self.think_sig = sig
                out.append(sse("content_block_delta", {
                    "type": "content_block_delta", "index": self.index,
                    "delta": {"type": "signature_delta", "signature": sig}}))
            return out

        if kind == "toolUseEvent" or "toolUseId" in data or "name" in data:
            return self._tool_event(data)

        if kind == "metadataEvent":
            # Kiro can decline a conversation outright. It still returns 200
            # with no content, so without this the client gets a blank message
            # and no reason at all.
            details = data.get("stopDetails") or {}
            refusal = details.get("refusal") if isinstance(details, dict) else None
            if isinstance(refusal, dict):
                why = (refusal.get("explanation")
                       or "Kiro declined to continue this conversation.")
                cat = refusal.get("category")
                text = "[kiro refusal%s] %s" % (
                    " " + cat if cat else "", why)
                log("refusal from kiro (%s): %s" % (cat, why))
                out = self._open_text()
                self.text_buf.append(text)
                self.stop_reason = "refusal"
                out.append(sse("content_block_delta", {
                    "type": "content_block_delta", "index": self.index,
                    "delta": {"type": "text_delta", "text": text}}))
                return out
            return []

        if kind == "contextUsageEvent":
            # Kiro reports true context usage; the Anthropic token counts this
            # proxy returns are a length/4 guess, so log the real number.
            pct = data.get("contextUsagePercentage")
            if isinstance(pct, (int, float)):
                self.context_pct = pct
            return []

        if kind == "exception":
            raise KiroError(400, json.dumps(data))

        return []

    def _tool_event(self, data):
        out = []
        name = data.get("name")
        tool_id = data.get("toolUseId")

        if name and (self.open_kind != "tool" or
                     (self.tool and self.tool["id"] != tool_id)):
            out.extend(self._close())
            self.index += 1
            self.open_kind = "tool"
            self.tool = {"id": tool_id or ("toolu_" + uuid.uuid4().hex[:20]),
                         "name": self.names.restore(name), "json": ""}
            out.append(sse("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "tool_use", "id": self.tool["id"],
                                  "name": self.tool["name"], "input": {}}}))
            first = data.get("input")
            if isinstance(first, dict) and first:
                first = json.dumps(first)
            if isinstance(first, str) and first:
                self.tool["json"] += first
                out.append(sse("content_block_delta", {
                    "type": "content_block_delta", "index": self.index,
                    "delta": {"type": "input_json_delta", "partial_json": first}}))
        elif self.open_kind == "tool" and "input" in data:
            frag = data.get("input")
            if isinstance(frag, dict):
                frag = json.dumps(frag) if frag else ""
            frag = frag or ""
            if frag:
                self.tool["json"] += frag
                out.append(sse("content_block_delta", {
                    "type": "content_block_delta", "index": self.index,
                    "delta": {"type": "input_json_delta", "partial_json": frag}}))

        if data.get("stop"):
            out.extend(self._close())
        return out

    def finish(self):
        """Closes any open block and returns (events, output_tokens)."""
        out = self._close()
        if not self.blocks:
            self.blocks.append({"type": "text", "text": ""})
        output_tokens = rough_tokens("".join(
            b.get("text") or b.get("thinking") or json.dumps(b.get("input", {}))
            for b in self.blocks))
        out.append(sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens}}))
        out.append(sse("message_stop", {"type": "message_stop"}))
        return out, output_tokens

    def start_event(self, input_tokens):
        return sse("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1}}})

    def final_message(self, input_tokens, output_tokens):
        return {
            "id": self.id, "type": "message", "role": "assistant",
            "model": self.model, "content": self.blocks,
            "stop_reason": self.stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }


def sse(event, data):
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode()


def rough_tokens(text):
    return max(1, len(text) // 4)


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Claude Code drops idle keep-alive sockets; that is not an error.
        kind = sys.exc_info()[0]
        if kind in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        log(traceback.format_exc())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kiroproxy/1.0"

    def log_message(self, fmt, *args):
        vlog("http " + fmt % args)

    # -- helpers --------------------------------------------------------
    def _json(self, status, obj):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status, message):
        log("error %s: %s" % (status, message))
        self._json(status, {"type": "error",
                            "error": {"type": "api_error", "message": message}})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b"{}"

    # -- routes ---------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/health", "/"):
            self._json(200, {"status": "ok", "region": self.server.creds.region})
        elif path == "/v1/models":
            self._json(200, {"data": [
                {"id": m, "type": "model", "display_name": m} for m in KNOWN_MODELS]})
        else:
            self._error(404, "not found: " + path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception as e:
            return self._error(400, "bad json: %s" % e)

        if path == "/v1/messages/count_tokens":
            text = content_to_text(body.get("system")) + json.dumps(
                body.get("messages") or [])
            return self._json(200, {"input_tokens": rough_tokens(text)})
        if path != "/v1/messages":
            return self._error(404, "not found: " + path)

        try:
            self._messages(body)
        except KiroError as e:
            try:
                self._error(502 if e.status >= 500 else e.status,
                            "kiro rejected the request (%s): %s" % (e.status, e.detail))
            except Exception:
                pass
        except BrokenPipeError:
            vlog("client went away")
        except Exception as e:
            log(traceback.format_exc())
            try:
                self._error(500, "%s: %s" % (type(e).__name__, e))
            except Exception:
                pass

    def _messages(self, body):
        creds = self.server.creds
        names = ToolNames()
        convo = hashlib.sha1(
            json.dumps(body.get("messages", [])[:1]).encode()).hexdigest()[:16]
        payload, model_id, system_prompt = build_payload(body, names, convo)
        budget = budget_for(convo)
        if budget:
            fit_payload(payload, system_prompt, budget)
        prompt_tokens = rough_tokens(json.dumps(payload))
        stream = bool(body.get("stream"))
        builder = ResponseBuilder(body.get("model") or model_id,
                                  "msg_" + uuid.uuid4().hex[:24], names)

        log("-> %s (%s, %d msgs, %d tools)%s"
            % (model_id, body.get("model"), len(body.get("messages") or []),
               len(body.get("tools") or []), " stream" if stream else ""))

        # Kiro's real limit is tokens, not bytes, and how the content tokenizes
        # varies far too much to predict from size alone. So when it says the
        # content is too long, cut the oldest history and ask again rather than
        # failing the turn. This is a backstop: at the default cap Claude Code's
        # own compaction, which leaves a summary behind, gets there first.
        conn = resp = None
        for attempt in range(4):
            try:
                conn, resp = call_kiro(creds, payload)
                break
            except KiroError as e:
                if ("CONTENT_LENGTH_EXCEEDS_THRESHOLD" not in e.detail
                        or attempt == 3):
                    raise
                state = payload["conversationState"]
                history = state.get("history") or []
                if len(history) < 2:
                    raise
                shrunk = int(len(json.dumps(state).encode()) * 0.7)
                cut = fit_payload(payload, system_prompt, shrunk)
                log("kiro says content too long; dropped %d history entries "
                    "and retrying (attempt %d)" % (cut, attempt + 2))
                if not cut:
                    raise
        decoder = EventStreamDecoder()
        headers_sent = False

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            headers_sent = True
            self._chunk(builder.start_event(prompt_tokens))
            self._chunk(sse("ping", {"type": "ping"}))

        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                for kind, data in decoder.feed(chunk):
                    for event in builder.handle(kind, data):
                        if stream:
                            self._chunk(event)
        except KiroError as e:
            # Mid-stream failure: the 200 and its chunks are already out, so a
            # fresh status line would corrupt the response. Report in-band.
            if not headers_sent:
                raise
            log("mid-stream error: %s" % e.detail[:300])
            self._chunk(sse("error", {"type": "error", "error": {
                "type": "api_error", "message": e.detail[:2000]}}))
            self._chunk(sse("message_stop", {"type": "message_stop"}))
            self._chunk(b"")
            return
        finally:
            conn.close()

        tail, out_tokens = builder.finish()
        if stream:
            for event in tail:
                self._chunk(event)
            self._chunk(b"")   # terminating chunk
        else:
            self._json(200, builder.final_message(prompt_tokens, out_tokens))
        if builder.context_pct is not None:
            remember_context(convo, builder.context_pct)
        used = ("" if builder.context_pct is None
                else ", context %.1f%%" % builder.context_pct)
        log("<- %s, %d blocks, stop=%s%s"
            % (model_id, len(builder.blocks), builder.stop_reason, used))

    def _chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="Anthropic API -> Kiro proxy")
    ap.add_argument("--port", type=int, default=int(os.environ.get("KIRO_PORT", 9100)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    creds = Credentials(args.db)
    creds.token()   # fail fast if login is stale

    server = Server((args.host, args.port), Handler)
    server.creds = creds
    log("kiroproxy on http://%s:%d  (region %s, default model %s)"
        % (args.host, args.port, creds.region, DEFAULT_MODEL))
    log("point Claude Code at it:  ANTHROPIC_BASE_URL=http://%s:%d"
        % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
