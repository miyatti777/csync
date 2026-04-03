#!/usr/bin/env python3
"""
csync — Confluence Sync Tool
Confluence Cloud ページ配下をローカル Markdown にミラーリングする。
- Pull: Confluence XHTML Storage → Markdown
- Push: Markdown → Confluence XHTML Storage
- Sync: 双方向差分同期
nsync (Notion Sync Tool) のアーキテクチャを Confluence Cloud REST API v2 向けに移植。
"""

__version__ = "0.1.0"

import atexit
import base64
import hashlib
import json
import mimetypes
import os
import re
import signal
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ==========================================
# Config
# ==========================================

class Config:
    def __init__(self):
        self.root_page_id = ""
        self.confluence_base_url = ""
        self.label = ""
        self.base_output_dir = Path(".")
        self.sync_dir = Path(".")
        self.tree_json = Path(".")
        self.sync_state_json = Path(".")
        self.crawl_max_depth = 10
        self.rate_limit_delay = 0.35
        self.max_retries = 3
        self.retry_backoff = 2.0
        self.exclude_paths = ["_csync", "_nsync", "_archived", "_sync", "_backup"]

CFG = Config()

EMAIL = ""
API_TOKEN = ""
HEADERS = {}

_api_stats = {"calls": 0, "rate_limits": 0, "errors": 0, "skipped": 0}


class RateLimitExhausted(Exception):
    """Raised when too many consecutive 429s indicate we should stop."""
    pass


def init_api():
    global EMAIL, API_TOKEN, HEADERS
    EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
    API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")
    if EMAIL and API_TOKEN:
        credentials = base64.b64encode(("%s:%s" % (EMAIL, API_TOKEN)).encode()).decode()
        HEADERS = {
            "Authorization": "Basic " + credentials,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    else:
        HEADERS = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


def load_config(config_path):
    p = Path(config_path).resolve()
    if not p.exists():
        print("ERROR: Config not found: %s" % p, flush=True)
        sys.exit(1)

    raw = p.read_text(encoding="utf-8")
    if HAS_YAML:
        data = yaml.safe_load(raw)
    else:
        data = _parse_simple_yaml(raw)

    CFG.root_page_id = str(data.get("root_page_id", ""))
    CFG.confluence_base_url = data.get("confluence_base_url", "")
    CFG.label = data.get("label", "Confluence")
    CFG.base_output_dir = p.parent
    CFG.sync_dir = p.parent / "_csync"
    CFG.tree_json = CFG.sync_dir / "tree_cache.json"
    CFG.sync_state_json = CFG.sync_dir / "sync_state.json"
    CFG.crawl_max_depth = data.get("crawl_max_depth", 10)
    CFG.rate_limit_delay = data.get("rate_limit_delay", 0.35)
    CFG.max_retries = data.get("max_retries", 3)
    CFG.retry_backoff = data.get("retry_backoff", 2.0)
    CFG.exclude_paths = data.get("exclude_paths", ["_csync", "_nsync", "_archived", "_sync", "_backup"])
    _migrate_sync_dir(p.parent)


def _migrate_sync_dir(base_dir):
    """Auto-migrate legacy _sync/ to _csync/ if the new dir does not exist."""
    old = base_dir / "_sync"
    new = base_dir / "_csync"
    if old.is_dir() and not new.exists():
        other_config_exists = (base_dir / ".nsync.yaml").exists()
        if other_config_exists:
            print("WARN: _sync/ exists but both .csync.yaml and .nsync.yaml found. Manual migration needed.", flush=True)
            return
        old.rename(new)
        print("Migrated: _sync/ -> _csync/", flush=True)


def _parse_simple_yaml(text):
    """PyYAML が無い環境用の簡易パーサー"""
    data = {}
    current_list_key = None
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            current_list_key = None
            continue
        if stripped.startswith("- ") and current_list_key:
            data.setdefault(current_list_key, []).append(stripped[2:].strip().strip('"').strip("'"))
            continue
        current_list_key = None
        if ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not val:
                current_list_key = key
                continue
            if val.lower() == "true":
                data[key] = True
            elif val.lower() == "false":
                data[key] = False
            else:
                try:
                    if "." in val:
                        data[key] = float(val)
                    else:
                        data[key] = int(val)
                except ValueError:
                    data[key] = val
    return data


# ==========================================
# Utilities
# ==========================================

def sanitize_filename(name):
    name = name.replace('\uff0f', '_').replace('\uff1a', '_').replace('\uff3c', '_')
    name = name.replace('\u2215', '_').replace('\uff5c', '_')
    name = re.sub(r'[<>:"/\\|?*/]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > 200:
        name = name[:200]
    return name


def extract_page_id_from_url(url):
    """Confluence URL からページ ID を抽出（数値）"""
    match = re.search(r'/pages/(\d+)', url)
    if match:
        return match.group(1)
    if re.match(r'^\d+$', url.strip()):
        return url.strip()
    return ""


def extract_base_url_from_url(url):
    """Confluence URL からベース URL（/wiki まで）を抽出"""
    match = re.match(r'(https?://[^/]+/wiki)', url)
    if match:
        return match.group(1)
    match = re.match(r'(https?://[^/]+)', url)
    if match:
        return match.group(1) + "/wiki"
    return ""


# ==========================================
# Crash Detection & Heartbeat
# ==========================================

_crawl_state = {"items": None, "queue": None, "root_page_id": "", "active": False}

def _heartbeat_path():
    return Path(CFG.base_output_dir) / "_csync" / "heartbeat"

def _write_heartbeat():
    try:
        hb = _heartbeat_path()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(datetime.now().isoformat() + "\n")
    except Exception:
        pass

def _remove_heartbeat():
    try:
        _heartbeat_path().unlink(missing_ok=True)
    except Exception:
        pass

CHECKPOINT_INTERVAL = 50

def _checkpoint_path():
    return CFG.sync_dir / "crawl_checkpoint.json"

def _save_checkpoint(items, queue, root_page_id=""):
    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    cp = {
        "root_page_id": str(root_page_id or ""),
        "items": items,
        "queue": queue,
        "saved_at": datetime.now().isoformat(),
    }
    with open(_checkpoint_path(), "w") as f:
        json.dump(cp, f, ensure_ascii=False)

def _load_checkpoint():
    p = _checkpoint_path()
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None

def _remove_checkpoint():
    p = _checkpoint_path()
    if p.exists():
        p.unlink()

def _emergency_save():
    if _crawl_state["active"] and _crawl_state["items"] is not None:
        try:
            _save_checkpoint(
                _crawl_state["items"],
                _crawl_state["queue"] or [],
                _crawl_state.get("root_page_id", ""),
            )
            print("\n  [emergency checkpoint saved on exit: %d items]" %
                  len(_crawl_state["items"]), flush=True)
        except Exception:
            pass
    _remove_heartbeat()
    _print_api_stats()

def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    print("\n  Received %s — saving state..." % sig_name, flush=True)
    _emergency_save()
    sys.exit(128 + signum)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
atexit.register(_emergency_save)


# ==========================================
# Confluence API (REST API v2, Basic Auth)
# ==========================================

def _api_url(path):
    """Build full API URL from relative path."""
    base = CFG.confluence_base_url.rstrip("/")
    if path.startswith("/"):
        return base + path
    return base + "/api/v2/" + path


def _api_request(url, method="GET", body_dict=None, retries=None):
    """Unified API request with Retry-After support and rate limit tracking."""
    if retries is None:
        retries = CFG.max_retries
    data_bytes = json.dumps(body_dict).encode() if body_dict else None
    consecutive_429 = 0

    for attempt in range(retries):
        req = urllib.request.Request(url, data=data_bytes, headers=HEADERS, method=method)
        _api_stats["calls"] += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                if not body:
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _api_stats["rate_limits"] += 1
                consecutive_429 += 1
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(float(retry_after), 1.0)
                    except ValueError:
                        wait = CFG.retry_backoff ** (attempt + 1)
                else:
                    wait = CFG.retry_backoff ** (attempt + 1)
                print("  Rate limited (429 #%d). Waiting %.1fs..." % (
                    _api_stats["rate_limits"], wait), flush=True)
                if consecutive_429 >= retries:
                    _api_stats["errors"] += 1
                    raise RateLimitExhausted(
                        "Too many consecutive 429s (%d). Stopping to avoid API ban." % consecutive_429)
                time.sleep(wait)
                continue
            elif e.code == 404:
                return None
            elif e.code == 409:
                _api_stats["errors"] += 1
                print("  Version conflict (409) on attempt %d: %s" % (attempt + 1, url), flush=True)
                if attempt < retries - 1:
                    time.sleep(CFG.retry_backoff ** attempt)
                    continue
                return None
            else:
                _api_stats["errors"] += 1
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                print("  HTTP %d on attempt %d: %s" % (e.code, attempt + 1, url), flush=True)
                if err_body:
                    print("  Response: %s" % err_body, flush=True)
                if attempt < retries - 1:
                    time.sleep(CFG.retry_backoff ** attempt)
                    continue
                return None
        except RateLimitExhausted:
            raise
        except Exception as e:
            _api_stats["errors"] += 1
            print("  Error on attempt %d: %s" % (attempt + 1, e), flush=True)
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            return None
    return None


def api_get(path_or_url, retries=None):
    url = path_or_url if path_or_url.startswith("http") else _api_url(path_or_url)
    return _api_request(url, "GET", retries=retries)


def api_post(path_or_url, body_dict, retries=None):
    url = path_or_url if path_or_url.startswith("http") else _api_url(path_or_url)
    return _api_request(url, "POST", body_dict, retries=retries)


def api_put(path_or_url, body_dict, retries=None):
    url = path_or_url if path_or_url.startswith("http") else _api_url(path_or_url)
    return _api_request(url, "PUT", body_dict, retries=retries)


def api_delete(path_or_url, retries=None):
    url = path_or_url if path_or_url.startswith("http") else _api_url(path_or_url)
    return _api_request(url, "DELETE", retries=retries)


MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def _api_request_binary(full_url, retries=None):
    """GET raw bytes (attachments, exports)."""
    if retries is None:
        retries = CFG.max_retries
    for attempt in range(retries):
        req = urllib.request.Request(full_url, headers=HEADERS, method="GET")
        _api_stats["calls"] += 1
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            _api_stats["errors"] += 1
            if e.code == 429:
                _api_stats["rate_limits"] += 1
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            raise
        except Exception as e:
            _api_stats["errors"] += 1
            if attempt < retries - 1:
                time.sleep(CFG.retry_backoff ** attempt)
                continue
            raise
    return None


def _abs_wiki_url(path_fragment):
    """Join Confluence base URL with an API/path fragment (e.g. download link)."""
    base = CFG.confluence_base_url.rstrip("/")
    frag = str(path_fragment or "").strip()
    if not frag:
        return ""
    if frag.startswith("http://") or frag.startswith("https://"):
        return frag
    if frag.startswith("/download/") and base.endswith("/wiki"):
        return base + frag
    if frag.startswith("/"):
        site = base[:-5] if base.endswith("/wiki") else base
        return site + frag
    return base + "/" + frag.lstrip("/")


def fetch_page_with_storage(page_id):
    """GET page JSON including body.storage (XHTML). Returns dict or None."""
    page_id = str(page_id or "").strip()
    if not page_id:
        return None
    path = "/api/v2/pages/%s?body-format=storage" % page_id
    data = api_get(path)
    if not isinstance(data, dict):
        return None
    return data


def iter_page_attachments(page_id):
    """Yield attachment dicts from v2 pages/{id}/attachments."""
    page_id = str(page_id or "").strip()
    if not page_id:
        return
    base_path = "/api/v2/pages/%s/attachments" % page_id
    for att in api_get_paginated(base_path, limit=50):
        if isinstance(att, dict):
            yield att


def _attachment_download_url(att):
    if not isinstance(att, dict):
        return ""
    link = att.get("downloadLink") or ""
    if not link:
        links = att.get("_links") or {}
        dl = links.get("download") if isinstance(links, dict) else None
        if isinstance(dl, dict):
            link = dl.get("href") or ""
    return str(link or "").strip()


def download_page_attachments(page_id, assets_dir, ctx=None):
    """
    Download all attachments into assets_dir. Returns:
      { original_filename -> relative path from markdown file dir, e.g. _assets/foo.png }
    """
    page_id = str(page_id or "").strip()
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    rel_prefix = assets_dir.name
    used_names = {}
    mapping = {}

    for att in iter_page_attachments(page_id):
        title = str(att.get("title") or "file").strip() or "file"
        dl = _attachment_download_url(att)
        if not dl:
            if ctx:
                ctx.warn("attachment_missing_link", tag="attachment", snippet=title[:120])
            continue
        size = int(att.get("fileSize") or 0)
        if size > MAX_ATTACHMENT_BYTES:
            if ctx:
                ctx.warn(
                    "attachment_too_large skip: %s" % title,
                    tag="attachment",
                    snippet=str(size),
                )
            continue

        local_name = sanitize_filename(title)
        base_name = local_name
        n = used_names.get(base_name, 0)
        if n:
            stem = Path(base_name).stem
            suf = Path(base_name).suffix or ""
            local_name = "%s_%d%s" % (stem, n, suf)
        used_names[base_name] = n + 1

        dest = assets_dir / local_name
        full_url = _abs_wiki_url(dl)
        try:
            data = _api_request_binary(full_url)
            if not data:
                if ctx:
                    ctx.warn("attachment_empty", tag="attachment", snippet=title)
                continue
            dest.write_bytes(data)
            mapping[title] = "%s/%s" % (rel_prefix, local_name)
            mapping[local_name] = "%s/%s" % (rel_prefix, local_name)
        except Exception as e:
            if ctx:
                ctx.warn("attachment_dl_failed: %s" % e, tag="attachment", snippet=title[:120])
            continue
        time.sleep(CFG.rate_limit_delay)

    return mapping


def resolve_attachment_links(markdown, asset_map, page_id=""):
    """
    Rewrite Confluence /download/attachments/... URLs to local _assets/ paths.
    asset_map keys: original title and saved filename -> '_assets/name'
    """
    md = str(markdown or "")
    if not md:
        return md

    def lookup_rel(fname):
        fn_clean = sanitize_filename(unquote(str(fname or "").strip()))
        for orig, rel in asset_map.items():
            if str(orig).startswith("_"):
                continue
            if sanitize_filename(str(orig)) == fn_clean:
                return rel
        return "_assets/%s" % fn_clean

    md = re.sub(
        r"https?://[^\s\)\]\"']+/download/attachments/(?:\d+/)+([^)\s\]\'\"]+)",
        lambda m: lookup_rel(m.group(1)),
        md,
        flags=re.I,
    )
    md = re.sub(
        r"\]\(/download/attachments/(?:\d+/)+([^)\s]+)\)",
        lambda m: "](%s)" % lookup_rel(m.group(1)),
        md,
        flags=re.I,
    )
    pid = str(page_id or "").strip()
    if pid:
        md = re.sub(
            r"https?://[^\s\)\]\"']+/download/attachments/%s/([^)\s\]\'\"]+)"
            % re.escape(pid),
            lambda m: lookup_rel(m.group(1)),
            md,
            flags=re.I,
        )
    return md


def api_get_paginated(path, limit=25):
    """Cursor-based pagination helper. Yields all results across pages."""
    separator = "&" if "?" in path else "?"
    url = _api_url(path) + separator + "limit=%d" % limit
    while url:
        resp = _api_request(url, "GET")
        if not resp:
            break
        results = resp.get("results", [])
        for item in results:
            yield item
        links = resp.get("_links", {})
        next_link = links.get("next")
        if next_link:
            base = CFG.confluence_base_url.rstrip("/")
            url = base + next_link if next_link.startswith("/") else next_link
        else:
            url = None
        if results:
            time.sleep(CFG.rate_limit_delay)


def iter_children_pages(parent_page_id, limit=100):
    """Yield all child pages under a parent page using Confluence API v2."""
    parent_page_id = str(parent_page_id).strip()
    if not parent_page_id:
        return

    path = "/api/v2/pages/%s/children" % parent_page_id
    for child in api_get_paginated(path, limit=limit):
        if not isinstance(child, dict):
            continue
        child_id = str(child.get("id", "")).strip()
        if not child_id:
            continue
        yield {
            "id": child_id,
            "title": child.get("title", "Untitled"),
            "parent_id": parent_page_id,
            "space_id": str(child.get("spaceId", "")) if child.get("spaceId") is not None else "",
            "status": child.get("status", ""),
        }


def list_child_pages(parent_page_id, limit=100):
    """Collect children pages into a list with stable metadata fields."""
    items = []
    seen = set()
    for child in iter_children_pages(parent_page_id, limit=limit):
        cid = child["id"]
        if cid in seen:
            continue
        seen.add(cid)
        items.append(child)
    return items


def _get_page_basic(page_id):
    """Fetch basic page metadata from Confluence API v2."""
    page_id = str(page_id).strip()
    if not page_id:
        return None
    data = api_get("/api/v2/pages/%s" % page_id)
    if not isinstance(data, dict):
        return None
    return {
        "id": str(data.get("id", page_id)),
        "title": data.get("title", "Untitled"),
        "space_id": str(data.get("spaceId", "")) if data.get("spaceId") is not None else "",
        "status": data.get("status", ""),
    }


def build_confluence_path(parent_path, title, page_id, used_paths=None):
    """Build a stable confluence_path with collision handling."""
    normalized = sanitize_filename(title or "Untitled")
    if not normalized:
        normalized = "untitled"

    base_path = (parent_path or "").strip("/")
    candidate = (base_path + "/" + normalized).strip("/")

    if used_paths is None:
        return candidate

    if candidate not in used_paths:
        used_paths.add(candidate)
        return candidate

    fallback = "%s-%s" % (candidate, page_id)
    if fallback not in used_paths:
        used_paths.add(fallback)
        return fallback

    i = 2
    while True:
        alt = "%s-%s-%d" % (candidate, page_id, i)
        if alt not in used_paths:
            used_paths.add(alt)
            return alt
        i += 1


def crawl_subtree_dfs(root_page_id, max_depth=None, resume=True, limit=100):
    """
    Crawl Confluence page subtree in DFS order.
    Returns a list of page metadata with stable confluence_path.
    """
    root_page_id = str(root_page_id).strip()
    if not root_page_id:
        raise ValueError("root_page_id is required")

    if max_depth is None:
        max_depth = CFG.crawl_max_depth

    items = []
    stack = []
    visited = set()
    used_paths = set()

    cp = _load_checkpoint() if resume else None
    cp_root = str((cp or {}).get("root_page_id", "")).strip()
    if cp and cp_root == root_page_id and isinstance(cp.get("items"), list) and isinstance(cp.get("queue"), list):
        items = cp["items"]
        stack = cp["queue"]
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id", "")).strip()
            path = str(item.get("confluence_path", "")).strip("/")
            if pid:
                visited.add(pid)
            if path:
                used_paths.add(path)
        print("Resuming crawl from checkpoint: %d items, %d queued" % (len(items), len(stack)), flush=True)
    else:
        root = _get_page_basic(root_page_id) or {
            "id": root_page_id,
            "title": "root-%s" % root_page_id,
            "space_id": "",
            "status": "",
        }
        root_path = build_confluence_path("", root.get("title", ""), root_page_id, used_paths)
        stack = [{
            "id": root_page_id,
            "title": root.get("title", "Untitled"),
            "parent_id": "",
            "space_id": root.get("space_id", ""),
            "status": root.get("status", ""),
            "depth": 0,
            "confluence_path": root_path,
        }]

    _crawl_state["active"] = True
    _crawl_state["items"] = items
    _crawl_state["queue"] = stack
    _crawl_state["root_page_id"] = root_page_id

    processed_since_checkpoint = 0
    while stack:
        node = stack.pop()
        node_id = str(node.get("id", "")).strip()
        if not node_id or node_id in visited:
            continue

        depth = int(node.get("depth", 0))
        if depth > max_depth:
            continue

        parent_id = str(node.get("parent_id", "")).strip()
        parent_path = str(node.get("confluence_path", "")).strip("/")
        if not parent_path:
            parent_path = build_confluence_path("", node.get("title", ""), node_id, used_paths)
            node["confluence_path"] = parent_path

        visited.add(node_id)
        items.append({
            "id": node_id,
            "title": node.get("title", "Untitled"),
            "parent_id": parent_id,
            "space_id": str(node.get("space_id", "")),
            "status": node.get("status", ""),
            "depth": depth,
            "confluence_path": parent_path,
        })

        children = list_child_pages(node_id, limit=limit)
        for child in reversed(children):
            child_id = child["id"]
            if child_id in visited:
                continue
            child_path = build_confluence_path(parent_path, child.get("title", ""), child_id, used_paths)
            stack.append({
                "id": child_id,
                "title": child.get("title", "Untitled"),
                "parent_id": node_id,
                "space_id": child.get("space_id", ""),
                "status": child.get("status", ""),
                "depth": depth + 1,
                "confluence_path": child_path,
            })

        _crawl_state["items"] = items
        _crawl_state["queue"] = stack
        processed_since_checkpoint += 1
        if processed_since_checkpoint >= CHECKPOINT_INTERVAL:
            _save_checkpoint(items, stack, root_page_id)
            processed_since_checkpoint = 0
            _write_heartbeat()

    _remove_checkpoint()
    _crawl_state["active"] = False
    _crawl_state["items"] = None
    _crawl_state["queue"] = None
    _crawl_state["root_page_id"] = ""
    return items


def normalize_traversal_item(item):
    """Normalize traversal output fields for SG2-2 consumer interface."""
    if not isinstance(item, dict):
        return None
    pid = str(item.get("id", "")).strip()
    if not pid:
        return None
    return {
        "id": pid,
        "title": str(item.get("title", "Untitled")),
        "parent_id": str(item.get("parent_id", "")),
        "confluence_path": str(item.get("confluence_path", "")).strip("/"),
    }


def normalize_traversal_items(items):
    """
    SG2-2向けにTraversal出力I/Fを固定する。
    Required fields: id, title, parent_id, confluence_path
    """
    normalized = []
    seen = set()
    for item in items or []:
        row = normalize_traversal_item(item)
        if not row:
            continue
        rid = row["id"]
        if rid in seen:
            continue
        seen.add(rid)
        normalized.append(row)
    return normalized


def validate_traversal_items(items):
    """
    Minimal verification for SG2-1 traversal outputs.
    Returns a report dict used by T216.
    """
    required = ("id", "title", "parent_id", "confluence_path")
    id_set = set()
    path_set = set()
    missing_required = 0
    duplicate_ids = 0
    duplicate_paths = 0
    invalid_parent_refs = 0

    for row in items or []:
        if not isinstance(row, dict):
            missing_required += 1
            continue
        if any(k not in row for k in required):
            missing_required += 1
            continue

        rid = str(row.get("id", "")).strip()
        parent_id = str(row.get("parent_id", "")).strip()
        path = str(row.get("confluence_path", "")).strip("/")

        if not rid or not path:
            missing_required += 1
            continue

        if rid in id_set:
            duplicate_ids += 1
        id_set.add(rid)

        if path in path_set:
            duplicate_paths += 1
        path_set.add(path)

        if parent_id and parent_id not in id_set:
            # parent may appear later in unordered data, so this is only a weak check.
            invalid_parent_refs += 1

    return {
        "count": len(items or []),
        "missing_required": missing_required,
        "duplicate_ids": duplicate_ids,
        "duplicate_paths": duplicate_paths,
        "invalid_parent_refs_weak_check": invalid_parent_refs,
        "required_fields": list(required),
        "passed_minimum": (
            missing_required == 0 and duplicate_ids == 0 and duplicate_paths == 0
        ),
    }


# ==========================================
# Front Matter (shared with SG2-SG4)
# ==========================================

def parse_front_matter(text):
    """Extract YAML front matter from Markdown text."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    if HAS_YAML:
        try:
            fm = yaml.safe_load(fm_text) or {}
        except Exception:
            fm = {}
    else:
        fm = _parse_simple_yaml(fm_text)
    return fm, body


def build_front_matter(data):
    """Build YAML front matter string."""
    lines = ["---"]
    for k, v in data.items():
        if isinstance(v, str):
            lines.append('%s: "%s"' % (k, v.replace('"', '\\"')))
        else:
            lines.append("%s: %s" % (k, v))
    lines.append("---")
    return "\n".join(lines)


CSYNC_OWNED_FIELDS = {
    "confluence_id", "title", "space_id", "version", "status",
    "updated_at", "csync_pulled_at", "csync_version", "parent_id",
    "space_key", "confluence_parent", "confluence_url",
    "pushed_at", "synced_at", "confluence_version",
}


def build_pull_front_matter_dict(page_id, page_json, existing_fm=None):
    """SG2-3: metadata dict for Pull output. Merges with existing frontmatter to preserve
    fields from other tools (e.g., nsync's notion_id, synced_at)."""
    page_json = page_json or {}
    ver = page_json.get("version") or {}
    if not isinstance(ver, dict):
        ver = {}
    updated_at = str(ver.get("createdAt") or ver.get("when") or "")
    parent = page_json.get("parentId")

    base = {}
    if isinstance(existing_fm, dict):
        for k, v in existing_fm.items():
            if k not in CSYNC_OWNED_FIELDS:
                base[k] = v

    csync_data = {
        "confluence_id": str(page_id),
        "title": str(page_json.get("title") or ""),
        "space_id": str(page_json.get("spaceId") or ""),
        "version": ver.get("number", 1) if isinstance(ver.get("number", 1), int) else 1,
        "status": str(page_json.get("status") or "current"),
        "updated_at": updated_at,
        "csync_pulled_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "csync_version": __version__,
    }
    if parent:
        csync_data["parent_id"] = str(parent)
    if page_json.get("space_key"):
        csync_data["space_key"] = str(page_json["space_key"])

    base.update(csync_data)
    return base


def merge_front_matter_and_body(fm_dict, body_md):
    """Prepend YAML front matter to markdown body."""
    fm = build_front_matter(fm_dict)
    body = str(body_md or "").lstrip("\n")
    return fm + "\n\n" + body


# ==========================================
# SG2-2: XHTML -> Markdown Core Foundation
# ==========================================

class ConversionContext:
    """Holds warnings/errors produced during conversion."""

    def __init__(self, page_id=""):
        self.page_id = str(page_id or "")
        self.warnings = []
        self.errors = []

    def warn(self, message, tag="", snippet=""):
        self.warnings.append(
            {
                "page_id": self.page_id,
                "tag": str(tag or ""),
                "message": str(message or ""),
                "snippet": str(snippet or "")[:200],
            }
        )

    def error(self, message, tag="", snippet=""):
        self.errors.append(
            {
                "page_id": self.page_id,
                "tag": str(tag or ""),
                "message": str(message or ""),
                "snippet": str(snippet or "")[:200],
            }
        )


class MarkdownBuffer:
    """Line-oriented buffer with blank-line normalization."""

    def __init__(self):
        self._lines = []

    def add_line(self, line=""):
        self._lines.append(str(line))

    def ensure_blank_line(self):
        if not self._lines:
            return
        if self._lines[-1] != "":
            self._lines.append("")

    def add_text_block(self, text):
        text = _normalize_inline_text(text)
        if not text:
            return
        self.add_line(text)
        self.ensure_blank_line()

    def render(self):
        out = list(self._lines)
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out).strip()


_MACRO_NAME_RE = re.compile(r'ac:name\s*=\s*"([^"]+)"', re.IGNORECASE)
_AC_MACRO_RE = re.compile(r"<\s*ac:structured-macro\b", re.IGNORECASE)


def _normalize_inline_text(text):
    if text is None:
        return ""
    text = unescape(str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _escape_md_inline(text):
    if text is None:
        return ""
    text = unescape(str(text))
    text = text.replace("\\", "\\\\")
    text = text.replace("[", "\\[").replace("]", "\\]")
    return text


def _tag_name(elem):
    tag = getattr(elem, "tag", "")
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1].lower()
    return tag.lower()


def _iter_node_text(node):
    """Collect node text + child tails as a flattened plain text."""
    chunks = []
    if node.text:
        chunks.append(node.text)
    for child in list(node):
        chunks.append(_iter_node_text(child))
        if child.tail:
            chunks.append(child.tail)
    return "".join(chunks)


def _render_inline_children(node, ctx):
    parts = []
    if node.text:
        parts.append(_escape_md_inline(node.text))
    for child in list(node):
        parts.append(_render_inline_node(child, ctx))
        if child.tail:
            parts.append(_escape_md_inline(child.tail))
    return "".join(parts)


def _render_inline_node(node, ctx):
    tag = _tag_name(node)
    if tag == "br":
        return "  \n"
    if tag == "a":
        href = (node.attrib.get("href") or "").strip()
        label = _normalize_inline_text(_render_inline_children(node, ctx))
        if not label:
            label = href or "link"
        if not href:
            return label
        return "[%s](%s)" % (label, href)
    if tag in ("strong", "b"):
        body = _normalize_inline_text(_render_inline_children(node, ctx))
        return "**%s**" % body if body else ""
    if tag in ("em", "i"):
        body = _normalize_inline_text(_render_inline_children(node, ctx))
        return "*%s*" % body if body else ""
    if tag == "code":
        body = _normalize_inline_text(_iter_node_text(node))
        if not body:
            return ""
        body = body.replace("`", "\\`")
        return "`%s`" % body
    if tag == "pre":
        code_text = _iter_node_text(node)
        code_text = unescape(str(code_text or "")).rstrip()
        if not code_text:
            return ""
        return "```\n%s\n```" % code_text
    # fallback: keep text content
    return _escape_md_inline(_iter_node_text(node))


def _collect_text_excluding_lists(node):
    """Collect text content from li while excluding nested list bodies."""
    parts = []
    if node.text:
        parts.append(_escape_md_inline(node.text))
    for child in list(node):
        ctag = _tag_name(child)
        if ctag in ("ul", "ol"):
            if child.tail:
                parts.append(_escape_md_inline(child.tail))
            continue
        parts.append(_render_inline_node(child, ctx=None))
        if child.tail:
            parts.append(_escape_md_inline(child.tail))
    return _normalize_inline_text("".join(parts))


def _render_list_node(list_node, buffer_obj, ctx, depth=0):
    ordered = _tag_name(list_node) == "ol"
    li_nodes = [n for n in list(list_node) if _tag_name(n) == "li"]
    if not li_nodes:
        text = _normalize_inline_text(_iter_node_text(list_node))
        if text:
            buffer_obj.add_text_block(text)
        return

    for index, li in enumerate(li_nodes, start=1):
        indent = "  " * max(depth, 0)
        marker = ("%d." % index) if ordered else "-"
        line_text = _collect_text_excluding_lists(li)
        if line_text:
            buffer_obj.add_line("%s%s %s" % (indent, marker, line_text))
        else:
            buffer_obj.add_line("%s%s" % (indent, marker))
        for child in list(li):
            ctag = _tag_name(child)
            if ctag in ("ul", "ol"):
                _render_list_node(child, buffer_obj, ctx, depth=depth + 1)


def _render_blockquote_node(node, buffer_obj, ctx):
    temp = MarkdownBuffer()
    if node.text and _normalize_inline_text(node.text):
        temp.add_text_block(node.text)
    for child in list(node):
        _render_node_base(child, temp, ctx)
        if child.tail and _normalize_inline_text(child.tail):
            temp.add_text_block(child.tail)

    quote_body = temp.render()
    if not quote_body:
        quote_body = _normalize_inline_text(_iter_node_text(node))
    if not quote_body:
        return
    for line in quote_body.split("\n"):
        if line.strip():
            buffer_obj.add_line("> %s" % line)
        else:
            buffer_obj.add_line(">")
    buffer_obj.ensure_blank_line()


def _escape_table_cell(text):
    text = _normalize_inline_text(text)
    if not text:
        return ""
    text = text.replace("|", r"\|")
    text = text.replace("\n", "<br>")
    return text


def _row_cells_from_tr(tr_node, ctx):
    cells = []
    for cell in list(tr_node):
        ctag = _tag_name(cell)
        if ctag not in ("th", "td"):
            continue
        value = _escape_table_cell(_render_inline_children(cell, ctx))
        cells.append(value)
    return cells


def _render_table_node(table_node, buffer_obj, ctx):
    header_rows = []
    body_rows = []
    direct_tr = []

    for child in list(table_node):
        ctag = _tag_name(child)
        if ctag == "thead":
            for tr in list(child):
                if _tag_name(tr) == "tr":
                    header_rows.append(_row_cells_from_tr(tr, ctx))
        elif ctag in ("tbody", "tfoot"):
            for tr in list(child):
                if _tag_name(tr) == "tr":
                    body_rows.append(_row_cells_from_tr(tr, ctx))
        elif ctag == "tr":
            direct_tr.append(_row_cells_from_tr(child, ctx))

    if direct_tr and not body_rows:
        body_rows = direct_tr

    all_rows = [r for r in (header_rows + body_rows) if r]
    if not all_rows:
        text = _normalize_inline_text(_iter_node_text(table_node))
        if text:
            buffer_obj.add_text_block(text)
        return

    max_cols = max(len(r) for r in all_rows)
    if max_cols <= 0:
        return

    def _normalize_row(row):
        row = list(row or [])
        while len(row) < max_cols:
            row.append("")
        return row[:max_cols]

    if header_rows and header_rows[0]:
        header = _normalize_row(header_rows[0])
        data_rows = [_normalize_row(r) for r in body_rows]
    else:
        header = _normalize_row(all_rows[0])
        data_rows = [_normalize_row(r) for r in all_rows[1:]]

    def _as_md_row(row):
        return "| " + " | ".join(row) + " |"

    buffer_obj.add_line(_as_md_row(header))
    buffer_obj.add_line("| " + " | ".join(["---"] * max_cols) + " |")
    for row in data_rows:
        buffer_obj.add_line(_as_md_row(row))
    buffer_obj.ensure_blank_line()


def _attr_ac(node, local):
    for k, v in (node.attrib or {}).items():
        if k == local or k.endswith("}" + local):
            return str(v or "")
    ac_key = "ac:" + local
    if ac_key in (node.attrib or {}):
        return str(node.attrib[ac_key] or "")
    return ""


def _macro_collect_params(node):
    out = {}
    for child in list(node):
        if _tag_name(child) != "parameter":
            continue
        pn = (_attr_ac(child, "name") or "").strip().lower()
        pv = _normalize_inline_text(_iter_node_text(child))
        if pn:
            out[pn] = pv
    return out


def _macro_plain_body(node):
    for child in list(node):
        if _tag_name(child) == "plain-text-body":
            return unescape(str(_iter_node_text(child) or "")).strip()
    return ""


def _macro_rich_body_md(node, ctx):
    for child in list(node):
        if _tag_name(child) != "rich-text-body":
            continue
        frags = []
        for sub in list(child):
            try:
                frags.append(ET.tostring(sub, encoding="unicode", method="xml"))
            except Exception:
                continue
        inner = "".join(frags).strip()
        if not inner:
            inner = _normalize_inline_text(_iter_node_text(child))
        if inner:
            sub_md, _ignored = xhtml_to_markdown_core(inner, page_id=ctx.page_id)
            return sub_md.strip()
    return ""


def _html_esc_summary(s):
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_structured_macro(node, buffer_obj, ctx):
    """SG2-3: Confluence ac:structured-macro → Markdown / HTML."""
    name = (_attr_ac(node, "name") or "").strip().lower()
    params = _macro_collect_params(node)
    plain = _macro_plain_body(node)
    rich = _macro_rich_body_md(node, ctx)
    body = plain or rich or ""

    alert_map = {
        "info": "INFO",
        "note": "NOTE",
        "warning": "WARNING",
        "tip": "TIP",
    }

    if name == "code":
        lang = params.get("language") or params.get("lang") or ""
        code_txt = plain or rich or ""
        code_txt = code_txt.replace("\r\n", "\n")
        fence = "```"
        if lang:
            buffer_obj.add_line("%s%s" % (fence, _normalize_inline_text(lang)))
        else:
            buffer_obj.add_line(fence)
        for line in code_txt.split("\n"):
            buffer_obj.add_line(line)
        buffer_obj.add_line(fence)
        buffer_obj.ensure_blank_line()
        return

    if name in alert_map:
        label = alert_map[name]
        inner = rich or plain
        buffer_obj.add_line("> [!%s]" % label)
        for line in inner.split("\n"):
            if line.strip():
                buffer_obj.add_line("> %s" % line)
            else:
                buffer_obj.add_line(">")
        buffer_obj.ensure_blank_line()
        return

    if name == "expand":
        title = params.get("title") or "Expand"
        inner = rich or plain
        buffer_obj.add_line("<details>")
        buffer_obj.add_line("<summary>%s</summary>" % _html_esc_summary(title))
        buffer_obj.add_line("")
        for line in inner.split("\n"):
            buffer_obj.add_line(line)
        buffer_obj.add_line("</details>")
        buffer_obj.ensure_blank_line()
        return

    if name == "panel":
        title = params.get("title") or "Panel"
        inner = rich or plain
        buffer_obj.add_line("> **%s**" % _normalize_inline_text(title))
        buffer_obj.add_line(">")
        for line in inner.split("\n"):
            if line.strip():
                buffer_obj.add_line("> %s" % line)
            else:
                buffer_obj.add_line(">")
        buffer_obj.ensure_blank_line()
        return

    ctx.warn("unsupported_macro: %s" % name, tag="structured-macro", snippet=name[:80])
    buffer_obj.add_line("<!-- unsupported macro: %s -->" % name)
    if body:
        buffer_obj.add_text_block(body)
    buffer_obj.ensure_blank_line()


def _fallback_unknown_node(node, buffer_obj, ctx, reason="Unhandled tag"):
    """
    Fallback priority:
    1) safe inline conversion
    2) plain text preservation
    3) raw HTML preservation
    """
    tag = _tag_name(node)

    # 1) Safe inline conversion
    safe_text = ""
    try:
        safe_text = _normalize_inline_text(_render_inline_children(node, ctx))
    except Exception:
        safe_text = ""
    if safe_text:
        ctx.warn("%s: safe_inline" % reason, tag=tag, snippet=safe_text)
        buffer_obj.add_text_block(safe_text)
        return

    # 2) Plain text preservation
    plain_text = ""
    try:
        plain_text = _normalize_inline_text(_iter_node_text(node))
    except Exception:
        plain_text = ""
    if plain_text:
        ctx.warn("%s: plain_text" % reason, tag=tag, snippet=plain_text)
        buffer_obj.add_text_block(plain_text)
        return

    # 3) Raw HTML preservation
    raw_html = ""
    try:
        raw_html = ET.tostring(node, encoding="unicode", method="xml")
        raw_html = str(raw_html or "").strip()
    except Exception:
        raw_html = ""
    if raw_html:
        ctx.warn("%s: raw_html" % reason, tag=tag, snippet=raw_html[:200])
        buffer_obj.add_line(raw_html)
        buffer_obj.ensure_blank_line()
        return

    ctx.warn("%s: dropped_empty_node" % reason, tag=tag, snippet="")


def _render_node_base(node, buffer_obj, ctx):
    """
    SG2-2/T221 foundation renderer.
    Only minimal block handling is implemented here.
    Detailed tag-specific rules are handled in T222+.
    """
    tag = _tag_name(node)
    try:
        if tag == "structured-macro":
            _render_structured_macro(node, buffer_obj, ctx)
            return
        text = _normalize_inline_text(_iter_node_text(node))
        inline_text = _normalize_inline_text(_render_inline_children(node, ctx))
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            if inline_text:
                buffer_obj.add_line("%s %s" % ("#" * level, inline_text))
                buffer_obj.ensure_blank_line()
            return
        if tag in ("p", "div", "section", "article"):
            if inline_text:
                buffer_obj.add_text_block(inline_text)
            return
        if tag == "pre":
            code_block = _render_inline_node(node, ctx)
            if code_block:
                for line in code_block.split("\n"):
                    buffer_obj.add_line(line)
                buffer_obj.ensure_blank_line()
            return
        if tag in ("br",):
            buffer_obj.add_line("")
            return
        if tag in ("ul", "ol"):
            _render_list_node(node, buffer_obj, ctx, depth=0)
            buffer_obj.ensure_blank_line()
            return
        if tag == "li":
            # Defensive handling: li appears unexpectedly outside ul/ol.
            text = _collect_text_excluding_lists(node)
            if text:
                buffer_obj.add_line("- %s" % text)
                buffer_obj.ensure_blank_line()
            return
        if tag == "blockquote":
            _render_blockquote_node(node, buffer_obj, ctx)
            return
        if tag == "table":
            _render_table_node(node, buffer_obj, ctx)
            return
        if tag in ("thead", "tbody", "tr", "th", "td"):
            # Defensive fallback: table fragments outside <table>.
            if text:
                buffer_obj.add_text_block(text)
            return
        if tag in ("root", "body", "html"):
            if node.text and _normalize_inline_text(node.text):
                buffer_obj.add_text_block(node.text)
            for child in list(node):
                _render_node_base(child, buffer_obj, ctx)
                if child.tail and _normalize_inline_text(child.tail):
                    buffer_obj.add_text_block(child.tail)
            return

        _fallback_unknown_node(node, buffer_obj, ctx, reason="Unhandled tag")
    except Exception as e:
        snippet = ""
        try:
            snippet = ET.tostring(node, encoding="unicode", method="xml")[:200]
        except Exception:
            snippet = _normalize_inline_text(_iter_node_text(node))[:200]
        ctx.error(str(e), tag=tag, snippet=snippet)
        _fallback_unknown_node(node, buffer_obj, ctx, reason="Render exception")


def _xhtml_fallback_strip(raw):
    """Best-effort XHTML tag stripping when ET parsing fails."""
    import re
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = re.sub(r"<p[^>]*>", "\n\n", text)
    text = re.sub(r"</p>", "", text)
    text = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "#" * int(m.group(1)) + " " + m.group(2), text)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text)
    text = re.sub(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"[\2](\1)", text)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"</?(?:ul|ol|div|span|table|tr|td|th|thead|tbody)[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def xhtml_to_markdown_core(xhtml, page_id=""):
    """
    Convert Confluence XHTML to Markdown (T221 foundation).
    Returns: (markdown_text, ConversionContext)
    """
    ctx = ConversionContext(page_id=page_id)
    buffer_obj = MarkdownBuffer()
    raw = str(xhtml or "").strip()
    if not raw:
        return "", ctx

    wrapped = (
        '<root xmlns:ac="urn:dummy-ac" xmlns:ri="urn:dummy-ri">'
        + raw
        + "</root>"
    )
    try:
        root = ET.fromstring(wrapped)
    except Exception as e:
        ctx.error("Failed to parse XHTML", tag="root", snippet=raw[:200])
        ctx.error(str(e), tag="root", snippet=raw[:120])
        fallback_text = _xhtml_fallback_strip(raw)
        return fallback_text, ctx

    for child in list(root):
        _render_node_base(child, buffer_obj, ctx)
        if child.tail and _normalize_inline_text(child.tail):
            buffer_obj.add_text_block(child.tail)

    return buffer_obj.render(), ctx


def _extract_sg3_macro_hints(raw_xhtml):
    raw = str(raw_xhtml or "")
    macro_names = _MACRO_NAME_RE.findall(raw)
    unique_names = []
    seen = set()
    for name in macro_names:
        key = name.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_names.append(key)
    return {
        "structured_macro_count": len(_AC_MACRO_RE.findall(raw)),
        "macro_names": unique_names,
        "has_ac_macro": bool(_AC_MACRO_RE.search(raw)),
    }


def build_sg3_handoff(page_id, raw_xhtml, markdown, ctx):
    """
    SG2-3連携用の固定I/Fを返す。
    SG2-2はcore変換のみを担当し、macro/asset統合はSG2-3へ委譲する。
    """
    return {
        "page_id": str(page_id or ""),
        "markdown": str(markdown or ""),
        "raw_xhtml": str(raw_xhtml or ""),
        "warnings": list(ctx.warnings if ctx else []),
        "errors": list(ctx.errors if ctx else []),
        "parse_ok": bool(ctx and not ctx.errors),
        "macro_hints": _extract_sg3_macro_hints(raw_xhtml),
        "handoff_version": "sg2-2.v1",
    }


def xhtml_to_markdown_with_handoff(xhtml, page_id=""):
    md, ctx = xhtml_to_markdown_core(xhtml, page_id=page_id)
    return build_sg3_handoff(page_id, xhtml, md, ctx)


# ==========================================
# SG2-3: Pull (single + recursive)
# ==========================================

def _space_key_for_id(space_id):
    sid = str(space_id or "").strip()
    if not sid:
        return ""
    data = api_get("/api/v2/spaces/%s" % sid)
    if isinstance(data, dict):
        return str(data.get("key") or "")
    return ""


def _body_storage_value(page_json):
    if not isinstance(page_json, dict):
        return ""
    body = page_json.get("body") or {}
    if not isinstance(body, dict):
        return ""
    st = body.get("storage") or {}
    if isinstance(st, dict):
        return str(st.get("value") or "")
    return ""


def confluence_path_to_output_path(confluence_path):
    """Map traversal confluence_path to Path under CFG.base_output_dir."""
    parts = [p for p in str(confluence_path or "").strip("/").split("/") if p]
    if not parts:
        return CFG.base_output_dir / "untitled.md"
    if len(parts) == 1:
        return CFG.base_output_dir / (sanitize_filename(parts[0]) + ".md")
    rel = Path(*[sanitize_filename(p) for p in parts[:-1]])
    stem = sanitize_filename(parts[-1])
    return CFG.base_output_dir / rel / (stem + ".md")


def cmd_pull_page_to_path(page_id, out_md_path, dry_run=False):
    """
    Pull one page: storage XHTML → MD + assets + front matter.
    Returns dict: ok, page_id, path, warnings, errors, message
    """
    page_id = str(page_id or "").strip()
    out_md_path = Path(out_md_path).resolve()
    result = {
        "ok": False,
        "page_id": page_id,
        "path": str(out_md_path),
        "warnings": [],
        "errors": [],
        "message": "",
    }
    if not page_id:
        result["message"] = "missing page_id"
        return result

    page_json = fetch_page_with_storage(page_id)
    if not isinstance(page_json, dict):
        result["message"] = "page fetch failed (404 or API error)"
        result["errors"].append(result["message"])
        return result

    xhtml = _body_storage_value(page_json)
    if page_json.get("spaceId") and not page_json.get("space_key"):
        sk = _space_key_for_id(page_json.get("spaceId"))
        if sk:
            page_json = dict(page_json)
            page_json["space_key"] = sk

    handoff = xhtml_to_markdown_with_handoff(xhtml, page_id=page_id)
    ctx_like = ConversionContext(page_id=page_id)
    for w in handoff.get("warnings") or []:
        ctx_like.warnings.append(w)
    md = handoff.get("markdown") or ""

    assets_dir = out_md_path.parent / "_assets"
    asset_map = {}
    if not dry_run:
        asset_map = download_page_attachments(page_id, assets_dir, ctx=ctx_like)
        md = resolve_attachment_links(md, asset_map, page_id=page_id)

    existing_fm = {}
    if out_md_path.exists():
        try:
            existing_fm, _ = parse_front_matter(out_md_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    fm = build_pull_front_matter_dict(page_id, page_json, existing_fm=existing_fm)
    full_md = merge_front_matter_and_body(fm, md)

    result["warnings"] = list(ctx_like.warnings)
    if dry_run:
        result["ok"] = True
        result["message"] = "dry-run OK"
        return result

    out_md_path.parent.mkdir(parents=True, exist_ok=True)
    out_md_path.write_text(full_md, encoding="utf-8")
    result["ok"] = True
    result["message"] = "written"
    return result


def cmd_pull_recursive(root_page_id, dry_run=False):
    """
    DFS crawl + cmd_pull_page_to_path per item. Returns summary dict.
    """
    root_page_id = str(root_page_id or "").strip()
    summary = {
        "ok": 0,
        "fail": 0,
        "skip": 0,
        "failures": [],
    }
    if not root_page_id:
        summary["failures"].append({"id": "", "error": "missing root_page_id"})
        return summary

    try:
        items = crawl_subtree_dfs(root_page_id, resume=True)
    except Exception as e:
        summary["failures"].append({"id": root_page_id, "error": str(e)})
        summary["fail"] += 1
        return summary

    for item in items:
        pid = str(item.get("id", "")).strip()
        cpath = str(item.get("confluence_path", "")).strip()
        if not pid:
            summary["skip"] += 1
            continue
        out_path = confluence_path_to_output_path(cpath)
        try:
            r = cmd_pull_page_to_path(pid, out_path, dry_run=dry_run)
            if r.get("ok"):
                summary["ok"] += 1
            else:
                summary["fail"] += 1
                summary["failures"].append({"id": pid, "error": r.get("message", "failed")})
        except Exception as e:
            summary["fail"] += 1
            summary["failures"].append({"id": pid, "error": str(e)})

    return summary


def _parse_pull_cli_args(args):
    """Return (recursive, dry_run, positional_tail)."""
    recursive = False
    dry_run = "--dry-run" in args
    rest = []
    for a in args:
        if a in ("--recursive", "-r"):
            recursive = True
        elif a == "--dry-run":
            pass
        else:
            rest.append(a)
    return recursive, dry_run, rest


# ==========================================
# SG3-1: Markdown -> XHTML Conversion Engine
# ==========================================

_ADMONITION_RE = re.compile(r'^>\s*\*\*(Info|Note|Warning|Tip)\*\*:\s*(.+)', re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r'^(\s*)([-*]|\d+\.)\s+(.*)$')
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')
_IMAGE_ONLY_RE = re.compile(r'^!\[([^\]]*)\]\(([^)]+)\)\s*$')


def _html_escape_text(text):
    text = str(text or "")
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def _html_escape_attr(text):
    text = _html_escape_text(text)
    return text.replace('"', "&quot;")


def _parse_table_cells(line):
    raw = str(line or "").strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    cells = re.split(r'(?<!\\)\|', raw)
    out = []
    for cell in cells:
        cell = cell.replace(r"\|", "|").strip()
        out.append(cell)
    return out


def _is_table_start(lines, idx):
    if idx + 1 >= len(lines):
        return False
    first = str(lines[idx] or "").strip()
    second = str(lines[idx + 1] or "").strip()
    if not first.startswith("|") or "|" not in first:
        return False
    if "|" not in second:
        return False
    cells = _parse_table_cells(second)
    if not cells:
        return False
    for c in cells:
        cc = c.replace(":", "").replace(" ", "")
        if len(cc) < 3 or set(cc) != {"-"}:
            return False
    return True


def _md_image_to_xhtml(alt, src):
    alt = str(alt or "")
    src = str(src or "").strip()
    if not src:
        return '<ac:image><ri:url ri:value="" /></ac:image>'
    if "/_assets/" in src or src.startswith("_assets/") or src.startswith("./_assets/"):
        filename = Path(src).name
        return (
            '<ac:image ac:alt="%s"><ri:attachment ri:filename="%s" /></ac:image>'
            % (_html_escape_attr(alt), _html_escape_attr(filename))
        )
    return (
        '<ac:image ac:alt="%s"><ri:url ri:value="%s" /></ac:image>'
        % (_html_escape_attr(alt), _html_escape_attr(src))
    )


def parse_inline_markdown_to_xhtml(text):
    """Markdown inline -> XHTML inline tags."""
    text = str(text or "")
    if not text:
        return ""

    placeholders = []

    def _hold(fragment):
        key = "__CSYNC_INLINE_%d__" % len(placeholders)
        placeholders.append(fragment)
        return key

    # Escape raw html-sensitive chars first.
    text = _html_escape_text(text)

    # Inline image
    text = re.sub(
        r'!\[([^\]]*)\]\(([^)]+)\)',
        lambda m: _hold(_md_image_to_xhtml(m.group(1), m.group(2))),
        text,
    )
    # Link
    text = re.sub(
        r'\[([^\]]+)\]\(([^)]+)\)',
        lambda m: _hold(
            '<a href="%s">%s</a>'
            % (_html_escape_attr(m.group(2)), _html_escape_text(m.group(1)))
        ),
        text,
    )
    # Code
    text = re.sub(
        r'`([^`]+)`',
        lambda m: _hold("<code>%s</code>" % _html_escape_text(m.group(1))),
        text,
    )
    # Strong+Em
    text = re.sub(
        r'\*\*\*(.+?)\*\*\*',
        lambda m: _hold("<strong><em>%s</em></strong>" % m.group(1)),
        text,
    )
    # Strong
    text = re.sub(
        r'\*\*(.+?)\*\*',
        lambda m: _hold("<strong>%s</strong>" % m.group(1)),
        text,
    )
    # Em
    text = re.sub(
        r'(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)',
        lambda m: _hold("<em>%s</em>" % m.group(1)),
        text,
    )
    # Strikethrough
    text = re.sub(
        r'~~(.+?)~~',
        lambda m: _hold('<span style="text-decoration: line-through;">%s</span>' % m.group(1)),
        text,
    )

    # Hard break: line ending two spaces in a paragraph line gets normalized before this stage.
    text = text.replace("  \n", "<br />\n")

    for i, frag in enumerate(placeholders):
        text = text.replace("__CSYNC_INLINE_%d__" % i, frag)
    return text


def _md_code_block_to_xhtml(lines, start_idx, lang):
    """Fence code block -> Confluence code macro. Returns (next_idx, xhtml)."""
    i = start_idx + 1
    buf = []
    while i < len(lines):
        line = str(lines[i] or "")
        if line.startswith("```"):
            i += 1
            break
        buf.append(line)
        i += 1
    code = "\n".join(buf).replace("]]>", "]]]]><![CDATA[>")
    lang = str(lang or "").strip()
    parts = ['<ac:structured-macro ac:name="code" ac:schema-version="1">']
    if lang:
        parts.append('<ac:parameter ac:name="language">%s</ac:parameter>' % _html_escape_text(lang))
    parts.append("<ac:plain-text-body><![CDATA[%s]]></ac:plain-text-body>" % code)
    parts.append("</ac:structured-macro>")
    return i, "".join(parts)


def _md_table_to_xhtml(lines, start_idx):
    """Markdown table -> XHTML table. Returns (next_idx, xhtml)."""
    header_cells = _parse_table_cells(lines[start_idx])
    i = start_idx + 2  # skip separator row
    rows = []
    while i < len(lines):
        line = str(lines[i] or "")
        if not line.strip().startswith("|"):
            break
        rows.append(_parse_table_cells(line))
        i += 1

    cols = len(header_cells)
    if cols <= 0:
        return i, "<p></p>"

    def _normalize(cells):
        data = list(cells or [])
        while len(data) < cols:
            data.append("")
        return data[:cols]

    header_cells = _normalize(header_cells)
    rows = [_normalize(r) for r in rows]

    parts = ["<table>", "<thead>", "<tr>"]
    for c in header_cells:
        parts.append("<th><p>%s</p></th>" % parse_inline_markdown_to_xhtml(c))
    parts.extend(["</tr>", "</thead>", "<tbody>"])
    for row in rows:
        parts.append("<tr>")
        for c in row:
            parts.append("<td><p>%s</p></td>" % parse_inline_markdown_to_xhtml(c))
        parts.append("</tr>")
    parts.extend(["</tbody>", "</table>"])
    return i, "".join(parts)


def _md_list_to_xhtml(lines, start_idx):
    """List block -> XHTML ul/ol. Returns (next_idx, xhtml)."""
    i = start_idx
    tokens = []
    while i < len(lines):
        m = _LIST_ITEM_RE.match(str(lines[i] or ""))
        if not m:
            break
        indent = len(m.group(1).replace("\t", "    "))
        depth = max(0, indent // 2)
        marker = m.group(2)
        ordered = marker.endswith(".")
        text = m.group(3)
        tokens.append((depth, ordered, text))
        i += 1

    if not tokens:
        return start_idx + 1, "<p>%s</p>" % parse_inline_markdown_to_xhtml(lines[start_idx])

    out = []
    stack = []  # list of dict(type="ul"/"ol", li_open=bool)

    def _close_depth(target_depth):
        while len(stack) > target_depth:
            top = stack.pop()
            if top["li_open"]:
                out.append("</li>")
            out.append("</%s>" % top["type"])

    for depth, ordered, text in tokens:
        want_type = "ol" if ordered else "ul"
        target_depth = depth + 1
        _close_depth(target_depth)

        while len(stack) < target_depth:
            out.append("<%s>" % want_type)
            stack.append({"type": want_type, "li_open": False})

        cur = stack[-1]
        if cur["type"] != want_type:
            if cur["li_open"]:
                out.append("</li>")
                cur["li_open"] = False
            out.append("</%s>" % cur["type"])
            out.append("<%s>" % want_type)
            cur["type"] = want_type

        if cur["li_open"]:
            out.append("</li>")
            cur["li_open"] = False
        out.append("<li>%s" % parse_inline_markdown_to_xhtml(text))
        cur["li_open"] = True

    _close_depth(0)
    return i, "".join(out)


def _md_admonition_to_xhtml(lines, start_idx, match):
    """Admonition blockquote -> macro. Returns (next_idx, xhtml)."""
    kind = str(match.group(1) or "").strip().lower()
    first = str(match.group(2) or "").strip()
    i = start_idx + 1
    body_lines = [first] if first else []
    while i < len(lines):
        line = str(lines[i] or "")
        if not line.startswith(">"):
            break
        body_lines.append(line[1:].lstrip())
        i += 1
    body_html = []
    for ln in body_lines:
        if ln.strip():
            body_html.append("<p>%s</p>" % parse_inline_markdown_to_xhtml(ln.strip()))
    if not body_html:
        body_html.append("<p></p>")
    xhtml = (
        '<ac:structured-macro ac:name="%s" ac:schema-version="1"><ac:rich-text-body>%s</ac:rich-text-body></ac:structured-macro>'
        % (_html_escape_attr(kind), "".join(body_html))
    )
    return i, xhtml


def _md_blockquote_to_xhtml(lines, start_idx):
    """Plain blockquote -> XHTML blockquote. Returns (next_idx, xhtml)."""
    i = start_idx
    q_lines = []
    while i < len(lines):
        line = str(lines[i] or "")
        if not line.startswith(">"):
            break
        q_lines.append(line[1:].lstrip())
        i += 1
    body = []
    for ln in q_lines:
        if ln.strip():
            body.append("<p>%s</p>" % parse_inline_markdown_to_xhtml(ln.strip()))
        else:
            body.append("<p></p>")
    return i, "<blockquote>%s</blockquote>" % "".join(body)


def _md_details_passthrough(lines, start_idx):
    """Pass through raw <details> block. Returns (next_idx, xhtml)."""
    i = start_idx
    buf = []
    while i < len(lines):
        line = str(lines[i] or "")
        buf.append(line)
        i += 1
        if "</details>" in line:
            break
    return i, "\n".join(buf)


def markdown_to_xhtml(md_text):
    """Markdown body text -> Confluence storage XHTML (rootless fragment)."""
    text = str(md_text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    parts = []
    i = 0

    while i < len(lines):
        line = str(lines[i] or "")
        stripped = line.strip()

        # empty line
        if not stripped:
            i += 1
            continue

        # code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i, xhtml = _md_code_block_to_xhtml(lines, i, lang)
            parts.append(xhtml)
            continue

        # table
        if stripped.startswith("|") and _is_table_start(lines, i):
            i, xhtml = _md_table_to_xhtml(lines, i)
            parts.append(xhtml)
            continue

        # admonition blockquote
        m_adm = _ADMONITION_RE.match(line)
        if m_adm:
            i, xhtml = _md_admonition_to_xhtml(lines, i, m_adm)
            parts.append(xhtml)
            continue

        # plain blockquote
        if line.startswith(">"):
            i, xhtml = _md_blockquote_to_xhtml(lines, i)
            parts.append(xhtml)
            continue

        # heading
        m_h = _HEADING_RE.match(stripped)
        if m_h:
            level = len(m_h.group(1))
            body = parse_inline_markdown_to_xhtml(m_h.group(2))
            parts.append("<h%d>%s</h%d>" % (level, body, level))
            i += 1
            continue

        # hr
        if re.match(r"^---+\s*$", stripped):
            parts.append("<hr />")
            i += 1
            continue

        # image-only line
        m_img = _IMAGE_ONLY_RE.match(stripped)
        if m_img:
            parts.append(_md_image_to_xhtml(m_img.group(1), m_img.group(2)))
            i += 1
            continue

        # list block
        if _LIST_ITEM_RE.match(line):
            i, xhtml = _md_list_to_xhtml(lines, i)
            parts.append(xhtml)
            continue

        # details passthrough
        if stripped.startswith("<details"):
            i, xhtml = _md_details_passthrough(lines, i)
            parts.append(xhtml)
            continue

        # paragraph: absorb adjacent plain lines
        para_lines = [line.strip()]
        j = i + 1
        while j < len(lines):
            nxt = str(lines[j] or "")
            nstrip = nxt.strip()
            if not nstrip:
                break
            if (
                nstrip.startswith("```")
                or (nstrip.startswith("|") and _is_table_start(lines, j))
                or _ADMONITION_RE.match(nxt)
                or nxt.startswith(">")
                or _HEADING_RE.match(nstrip)
                or re.match(r"^---+\s*$", nstrip)
                or _IMAGE_ONLY_RE.match(nstrip)
                or _LIST_ITEM_RE.match(nxt)
                or nstrip.startswith("<details")
            ):
                break
            para_lines.append(nxt.strip())
            j += 1
        i = j
        paragraph = " ".join([p for p in para_lines if p])
        parts.append("<p>%s</p>" % parse_inline_markdown_to_xhtml(paragraph))

    return "\n".join([p for p in parts if p]).strip()


# ==========================================
# SG3-2: Push Commands & Attachment Integration
# ==========================================

_RI_ATTACHMENT_RE = re.compile(r'ri:filename="([^"]+)"')


def _cfg_value(cfg, key, default=None):
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _iso_utc_now():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _confluence_v1_url(path):
    base = str(_cfg_value(CFG, "confluence_base_url", "") or "").rstrip("/")
    if not base:
        return ""
    return "%s%s" % (base, path if str(path).startswith("/") else ("/" + str(path)))


def _put_page_storage_with_status(page_id, body_dict):
    """
    PUT /api/v2/pages/{id} with explicit status handling.
    Returns: (ok: bool, payload: dict|None, status_code: int, message: str)
    """
    page_id = str(page_id or "").strip()
    if not page_id:
        return False, None, 0, "missing page id"
    url = _api_url("pages/%s" % page_id)
    data_bytes = json.dumps(body_dict).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=HEADERS, method="PUT")
    _api_stats["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return True, {}, resp.getcode(), ""
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            return True, payload, resp.getcode(), ""
    except urllib.error.HTTPError as e:
        _api_stats["errors"] += 1
        if e.code == 429:
            _api_stats["rate_limits"] += 1
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return False, None, e.code, body
    except Exception as e:
        _api_stats["errors"] += 1
        return False, None, 0, str(e)


def _write_front_matter_updates(md_path, fm, body, updates):
    """Update frontmatter fields and rewrite file. Preserves non-csync fields."""
    fm = dict(fm or {})
    for k, v in (updates or {}).items():
        fm[k] = v
    merged = merge_front_matter_and_body(fm, body)
    Path(md_path).write_text(merged, encoding="utf-8")


def _upload_attachment(cfg, page_id, filepath):
    """
    Upload a single file to Confluence attachment API (v1).
    Returns attachment dict {id, title, download_link} or None on failure.
    """
    page_id = str(page_id or "").strip()
    p = Path(filepath)
    if not page_id:
        print("WARN: _upload_attachment skipped (missing page_id)", flush=True)
        return None
    if not p.is_file():
        print("WARN: _upload_attachment skipped (file not found): %s" % p, flush=True)
        return None

    base_auth = HEADERS.get("Authorization", "")
    if not base_auth:
        print("WARN: _upload_attachment skipped (missing auth header)", flush=True)
        return None

    url = _confluence_v1_url("/rest/api/content/%s/child/attachment" % page_id)
    if not url:
        print("WARN: _upload_attachment skipped (missing confluence_base_url)", flush=True)
        return None

    filename = p.name
    ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    data = p.read_bytes()
    boundary = "----csync-boundary-%d" % int(time.time() * 1000)
    preamble = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n"
    ) % (boundary, filename, ctype)
    body = preamble.encode("utf-8") + data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")

    headers = {
        "Authorization": base_auth,
        "Accept": "application/json",
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "X-Atlassian-Token": "nocheck",
    }

    req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    _api_stats["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        payload = json.loads(raw) if raw else {}
        results = payload.get("results") if isinstance(payload, dict) else None
        first = results[0] if isinstance(results, list) and results else {}
        if not isinstance(first, dict):
            first = {}
        raw_links = first.get("_links")
        download_link = ""
        if isinstance(raw_links, dict):
            dl = raw_links.get("download")
            if isinstance(dl, str):
                download_link = dl
            elif isinstance(dl, dict):
                href = dl.get("href")
                if isinstance(href, str):
                    download_link = href
        return {
            "id": str(first.get("id") or ""),
            "title": str(first.get("title") or filename),
            "download_link": download_link,
        }
    except urllib.error.HTTPError as e:
        _api_stats["errors"] += 1
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        print("WARN: attachment upload failed (%s): %s" % (e.code, filename), flush=True)
        if body_text:
            print("  Response: %s" % body_text, flush=True)
        return None
    except Exception as e:
        _api_stats["errors"] += 1
        print("WARN: attachment upload error: %s" % e, flush=True)
        return None


def _resolve_local_images_for_push(cfg, page_id, md_filepath, xhtml, dry_run=False):
    """
    Upload local _assets files referenced by ri:attachment and keep XHTML intact.
    """
    page_id = str(page_id or "").strip()
    md_path = Path(md_filepath).resolve()
    xhtml = str(xhtml or "")
    if not page_id or not xhtml:
        return xhtml

    filenames = _RI_ATTACHMENT_RE.findall(xhtml)
    if not filenames:
        return xhtml

    assets_dir = md_path.parent / "_assets"
    uploaded = set()
    for filename in filenames:
        fname = str(filename or "").strip()
        if not fname or fname in uploaded:
            continue
        uploaded.add(fname)
        local_path = assets_dir / fname
        if not local_path.is_file():
            print("WARN: local attachment not found: %s" % local_path, flush=True)
            continue
        if dry_run:
            print("[DRY RUN] Would upload attachment: %s" % local_path, flush=True)
            continue
        result = _upload_attachment(cfg, page_id, local_path)
        if result:
            print("Uploaded attachment: %s" % fname, flush=True)
    return xhtml


def cmd_push(cfg, filepath, dry_run=False):
    """
    Push single markdown file to Confluence.
    Existing page update path with version.number handling and 409 retry.
    """
    md_path = Path(filepath).resolve()
    if not md_path.is_file():
        return {"ok": False, "message": "markdown not found: %s" % md_path}

    text = md_path.read_text(encoding="utf-8")
    fm, body = parse_front_matter(text)
    if not isinstance(fm, dict):
        fm = {}

    page_id = str(fm.get("confluence_id") or "").strip()
    if not page_id:
        if "cmd_push_new" in globals():
            return cmd_push_new(cfg, md_path, dry_run=dry_run)
        return {"ok": False, "message": "missing confluence_id (cmd_push_new not available yet)"}

    title = str(fm.get("title") or "").strip()
    if not title:
        title = md_path.stem

    xhtml = markdown_to_xhtml(body)
    final_xhtml = _resolve_local_images_for_push(cfg, page_id, md_path, xhtml, dry_run=dry_run)

    if dry_run:
        preview = final_xhtml[:240].replace("\n", "\\n")
        print("[DRY RUN] cmd_push target=%s page_id=%s" % (md_path, page_id), flush=True)
        print("[DRY RUN] XHTML preview: %s" % preview, flush=True)
        return {"ok": True, "message": "dry-run", "page_id": page_id}

    max_attempts = 3
    last_error = ""
    for attempt in range(max_attempts):
        page = fetch_page_with_storage(page_id)
        if not isinstance(page, dict):
            return {"ok": False, "message": "failed to fetch page %s" % page_id}
        ver = page.get("version") or {}
        current_version = int(ver.get("number") or 1)
        payload = {
            "id": page_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": final_xhtml},
            "version": {
                "number": current_version + 1,
                "message": "Pushed via csync",
            },
        }
        ok, resp, status_code, message = _put_page_storage_with_status(page_id, payload)
        if ok:
            new_version = current_version + 1
            if isinstance(resp, dict):
                rver = resp.get("version") or {}
                if isinstance(rver, dict) and isinstance(rver.get("number"), int):
                    new_version = rver.get("number")
            now = _iso_utc_now()
            _write_front_matter_updates(
                md_path,
                fm,
                body,
                {
                    "version": new_version,
                    "pushed_at": now,
                    "synced_at": now,
                    "csync_pulled_at": now,
                },
            )
            print("Pushed: %s (page %s, version %s)" % (md_path, page_id, new_version), flush=True)
            return {"ok": True, "message": "pushed", "page_id": page_id, "version": new_version}

        if status_code == 409:
            print("Version conflict (409). retrying %d/%d..." % (attempt + 1, max_attempts), flush=True)
            last_error = "409 conflict"
            time.sleep(0.3)
            continue

        last_error = "HTTP %s %s" % (status_code, message[:180] if message else "")
        break

    return {
        "ok": False,
        "message": "push failed for %s (page %s): %s" % (md_path, page_id, last_error or "unknown"),
    }


def _resolve_space_id(cfg, fm=None):
    fm = fm or {}
    fm_space = str(fm.get("space_id") or fm.get("spaceId") or "").strip()
    if fm_space:
        return fm_space
    cfg_space = str(_cfg_value(cfg, "space_id", "") or "").strip()
    if cfg_space:
        return cfg_space
    root_id = str(_cfg_value(cfg, "root_page_id", "") or "").strip()
    if root_id:
        root_page = api_get("pages/%s" % root_id)
        if isinstance(root_page, dict):
            space_id = str(root_page.get("spaceId") or "").strip()
            if space_id:
                return space_id
    return ""


def _resolve_parent_page_id(parent_ref):
    ref = str(parent_ref or "").strip()
    if not ref:
        return ""
    pid = extract_page_id_from_url(ref)
    if pid:
        return pid
    if ref.isdigit():
        return ref
    return ""


def _make_title_unique(title, filepath, tree=None):
    """Append [parent_dir] suffix when an identical title already exists in the space.

    Confluence enforces space-wide unique titles regardless of hierarchy.
    When multiple directories contain files with the same name (e.g.
    tasks.yaml.md), the second and subsequent pushes would overwrite the
    first page.  This function disambiguates by appending the parent
    directory name (or deeper path if still colliding).
    """
    if tree is None:
        tree = _load_tree_cache() or []
    if not tree:
        return title

    existing_titles = set()
    for item in tree:
        t = str(item.get("title", "")).strip()
        if t:
            existing_titles.add(t)

    if title not in existing_titles:
        return title

    p = Path(filepath).resolve()
    base = CFG.base_output_dir.resolve()
    try:
        rel = p.relative_to(base)
    except ValueError:
        return title

    parent_name = rel.parent.name if rel.parent.name else ""
    if not parent_name:
        return title

    new_title = "%s [%s]" % (title, parent_name)
    if new_title not in existing_titles:
        return new_title

    parts = list(rel.parent.parts)
    if len(parts) >= 2:
        path_suffix = str(Path(*parts[-2:]))
        new_title = "%s [%s]" % (title, path_suffix)
        if new_title not in existing_titles:
            return new_title

    full_suffix = str(rel.parent)
    return "%s [%s]" % (title, full_suffix)


def cmd_push_new(cfg, filepath, dry_run=False):
    """
    Create a new Confluence page from local markdown.
    """
    md_path = Path(filepath).resolve()
    if not md_path.is_file():
        return {"ok": False, "message": "markdown not found: %s" % md_path}

    text = md_path.read_text(encoding="utf-8")
    fm, body = parse_front_matter(text)
    if not isinstance(fm, dict):
        fm = {}

    title = str(fm.get("title") or "").strip() or md_path.stem
    parent_ref = str(fm.get("confluence_parent") or fm.get("parent_id") or "").strip()
    parent_id = _resolve_parent_page_id(parent_ref)
    if not parent_id:
        inferred = _infer_parent_from_path(md_path)
        if inferred:
            parent_id, inferred_title = inferred
            if not title or title == md_path.stem:
                title = inferred_title or title
            print("Inferred parent page %s from directory structure for: %s" % (parent_id, md_path.name), flush=True)
    if not parent_id:
        return {"ok": False, "message": "missing/invalid confluence_parent for new page: %s" % md_path}

    original_title = title
    title = _make_title_unique(title, md_path)
    if title != original_title:
        print("  Title deduplicated: '%s' → '%s'" % (original_title, title), flush=True)

    xhtml = markdown_to_xhtml(body)
    space_id = _resolve_space_id(cfg, fm=fm)
    if dry_run:
        if not space_id:
            space_id = "<unresolved>"
        preview = xhtml[:240].replace("\n", "\\n")
        print(
            "[DRY RUN] cmd_push_new target=%s parent=%s space=%s title=%s"
            % (md_path, parent_id, space_id, title),
            flush=True,
        )
        print("[DRY RUN] XHTML preview: %s" % preview, flush=True)
        return {"ok": True, "message": "dry-run"}

    if not space_id:
        return {"ok": False, "message": "space_id could not be resolved"}

    payload = {
        "spaceId": space_id,
        "parentId": parent_id,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": xhtml},
    }
    resp = api_post("pages", payload)

    page_id = ""
    title_collision = False
    if isinstance(resp, dict) and resp.get("id"):
        page_id = str(resp["id"]).strip()
    else:
        existing_id = _find_child_page_by_title(parent_id, title)
        if not existing_id:
            existing_id = _find_page_in_space_by_title(title)
        if existing_id:
            title_collision = True
            page_id = existing_id
            print("  Title '%s' already exists (page %s). Updating instead of creating." % (title, page_id), flush=True)
            page = fetch_page_with_storage(page_id)
            if isinstance(page, dict):
                ver = page.get("version") or {}
                current_version = int(ver.get("number") or 1)
                put_payload = {
                    "id": page_id,
                    "status": "current",
                    "title": title,
                    "body": {"representation": "storage", "value": xhtml},
                    "version": {"number": current_version + 1, "message": "Updated by csync (title collision resolved)"},
                }
                _put_page_storage_with_status(page_id, put_payload)
            else:
                return {"ok": False, "message": "title collision: found page %s but could not fetch it" % page_id}
        else:
            return {"ok": False, "message": "failed to create page (title '%s' may already exist elsewhere)" % title}

    final_xhtml = _resolve_local_images_for_push(cfg, page_id, md_path, xhtml, dry_run=False)
    if final_xhtml != xhtml:
        page = fetch_page_with_storage(page_id)
        if isinstance(page, dict):
            ver = page.get("version") or {}
            current_version = int(ver.get("number") or 1)
            put_payload = {
                "id": page_id,
                "status": "current",
                "title": title,
                "body": {"representation": "storage", "value": final_xhtml},
                "version": {"number": current_version + 1, "message": "Attach updates via csync"},
            }
            _put_page_storage_with_status(page_id, put_payload)

    created_page = fetch_page_with_storage(page_id)
    created_ver = 1
    if isinstance(created_page, dict):
        cv = created_page.get("version") or {}
        if isinstance(cv, dict) and isinstance(cv.get("number"), int):
            created_ver = cv["number"]

    now = _iso_utc_now()
    site = str(_cfg_value(cfg, "confluence_base_url", "") or "").rstrip("/")
    page_url = "%s/pages/%s" % (site, page_id) if site else ""
    updates = {
        "confluence_id": page_id,
        "space_id": space_id,
        "version": created_ver,
        "pushed_at": now,
        "synced_at": now,
        "csync_pulled_at": now,
    }
    if page_url:
        updates["confluence_url"] = page_url
    _write_front_matter_updates(md_path, fm, body, updates)
    action = "Updated (title collision)" if title_collision else "Created and pushed"
    print("%s: %s (page %s, version %s)" % (action, md_path, page_id, created_ver), flush=True)
    return {"ok": True, "message": "created", "page_id": page_id, "version": created_ver, "title": title}


def _collect_push_targets(root_md):
    root_md = Path(root_md).resolve()
    root_dir = root_md.parent
    targets = []
    for p in root_dir.rglob("*.md"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root_dir).parts
        if "_assets" in rel_parts or "_csync" in rel_parts or "_nsync" in rel_parts or "_sync" in rel_parts:
            continue
        targets.append(p)
    targets = sorted(targets, key=lambda p: (len(p.relative_to(root_dir).parts), str(p).lower()))
    if root_md in targets:
        targets.remove(root_md)
        targets.insert(0, root_md)
    return targets


def cmd_push_recursive(cfg, filepath, dry_run=False):
    """
    Push markdown tree rooted at filepath's directory.
    Uses hierarchical bootstrap to create parent directory pages as needed.
    """
    root_md = Path(filepath).resolve()
    if not root_md.is_file():
        return {"ok": False, "message": "markdown not found: %s" % root_md}

    targets = _collect_push_targets(root_md)
    summary = {"ok": 0, "fail": 0, "skip": 0, "failures": []}
    for md in targets:
        text = md.read_text(encoding="utf-8")
        fm, _body = parse_front_matter(text)
        if not isinstance(fm, dict):
            fm = {}
        has_id = str(fm.get("confluence_id") or "").strip() != ""
        has_parent = str(fm.get("confluence_parent") or "").strip() != ""
        if has_id:
            res = cmd_push(cfg, md, dry_run=dry_run)
        elif has_parent:
            res = cmd_push_new(cfg, md, dry_run=dry_run)
        else:
            if not dry_run:
                parent_id = _ensure_parent_hierarchy(md)
                if parent_id:
                    fm["confluence_parent"] = parent_id
                    _write_front_matter_updates(md, fm, _body, {"confluence_parent": parent_id})
            inferred = _infer_parent_from_path(md)
            if inferred:
                res = cmd_push_new(cfg, md, dry_run=dry_run)
            else:
                summary["skip"] += 1
                print("SKIP(no confluence_id/confluence_parent, inference failed): %s" % md, flush=True)
                continue

        if res.get("ok"):
            summary["ok"] += 1
            page_id = res.get("page_id", "")
            if page_id and not has_id:
                try:
                    rel = md.relative_to(CFG.base_output_dir)
                    is_index = (md.stem == md.parent.name)
                    if is_index:
                        cpath_parts = list(rel.parent.parts)
                    else:
                        cpath_parts = list(rel.parent.parts) + [rel.stem]
                    tree = _load_tree_cache() or []
                    root_title = ""
                    for item in tree:
                        if item.get("depth", -1) == 0:
                            root_title = item.get("title", "")
                            break
                    cpath = root_title + "/" + "/".join(cpath_parts) if root_title else "/".join(cpath_parts)
                    _save_tree_cache_entry({
                        "id": page_id,
                        "title": md.stem,
                        "confluence_path": cpath,
                        "depth": len(cpath_parts),
                    })
                except ValueError:
                    pass
        else:
            summary["fail"] += 1
            summary["failures"].append({"file": str(md), "error": res.get("message", "failed")})
            print("FAIL: %s :: %s" % (md, res.get("message", "failed")), flush=True)
    return summary


def _resolve_parent_for_new(cfg, parent_ref):
    pid = _resolve_parent_page_id(parent_ref)
    if pid:
        return pid

    # Try local title lookup from markdown front matter.
    root = Path(_cfg_value(cfg, "base_output_dir", ".")).resolve()
    wanted = str(parent_ref or "").strip().lower()
    if not wanted:
        return ""
    for p in root.rglob("*.md"):
        if "_assets" in p.parts or "_csync" in p.parts or "_nsync" in p.parts or "_sync" in p.parts:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _body = parse_front_matter(txt)
        if not isinstance(fm, dict):
            continue
        title = str(fm.get("title") or "").strip().lower()
        cid = str(fm.get("confluence_id") or "").strip()
        if title and cid and title == wanted:
            return cid
    return ""


def cmd_new(cfg, parent_ref, title):
    """
    Create local markdown scaffold for a new page (not pushed yet).
    """
    title = str(title or "").strip()
    if not title:
        return {"ok": False, "message": "title is required"}

    parent_id = _resolve_parent_for_new(cfg, parent_ref)
    if not parent_id:
        return {"ok": False, "message": "could not resolve parent: %s" % parent_ref}

    base_dir = Path(_cfg_value(cfg, "base_output_dir", ".")).resolve()
    safe = sanitize_filename(title) or "new-page"
    out = base_dir / (safe + ".md")
    idx = 2
    while out.exists():
        out = base_dir / ("%s-%d.md" % (safe, idx))
        idx += 1

    fm = {
        "title": title,
        "confluence_parent": parent_id,
        "created_at": _iso_utc_now(),
    }
    content = merge_front_matter_and_body(fm, "")
    out.write_text(content + "\n", encoding="utf-8")
    print('Created: %s (Run "csync.py push %s" to publish)' % (out, out), flush=True)
    return {"ok": True, "message": "created", "path": str(out)}


# ==========================================
# Sync — Bidirectional Sync Engine (SG4)
# ==========================================


def load_sync_state():
    """Load sync state from JSON file."""
    if CFG.sync_state_json.exists():
        try:
            with open(CFG.sync_state_json) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"items": {}, "last_sync": None}


def save_sync_state(state):
    """Persist sync state to JSON file."""
    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    with open(CFG.sync_state_json, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _strip_front_matter_bytes(raw):
    """Remove YAML front matter from raw bytes for hash comparison."""
    marker = b"---"
    if not raw.startswith(marker):
        return raw
    end = raw.find(b"\n---", 3)
    if end < 0:
        return raw
    rest = raw[end + 4:]
    return rest.lstrip(b"\n")


def _file_content_hash(filepath):
    """Compute SHA256 hash of file body (excluding front matter). Returns hex string or empty on error."""
    try:
        raw = Path(filepath).read_bytes()
        body = _strip_front_matter_bytes(raw)
        return hashlib.sha256(body).hexdigest()
    except OSError:
        return ""


def _get_page_version_info(page_id):
    """Fetch version info for remote change detection.

    Returns dict with version_number and updated_at, or None on failure.
    Uses the lightweight /pages/{id} endpoint (no body).
    """
    page_id = str(page_id or "").strip()
    if not page_id:
        return None
    data = api_get("/api/v2/pages/%s" % page_id)
    if not isinstance(data, dict):
        return None
    ver = data.get("version") or {}
    if not isinstance(ver, dict):
        return None
    return {
        "version_number": ver.get("number", 0),
        "updated_at": str(ver.get("createdAt") or ver.get("when") or ""),
    }


def _crawl_and_save():
    """Crawl page tree and persist to tree_cache.json. Returns item list."""
    items = crawl_subtree_dfs(CFG.root_page_id, resume=True)
    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    with open(CFG.tree_json, "w") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    print("Crawled %d pages, saved to %s" % (len(items), CFG.tree_json), flush=True)
    return items


def _load_tree_cache():
    """Load tree_cache.json. Returns item list or None."""
    if not CFG.tree_json.exists():
        return None
    try:
        with open(CFG.tree_json) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_tree_cache_entry(entry):
    """Append a new entry to tree_cache.json on disk and return updated list."""
    tree = _load_tree_cache() or []
    eid = str(entry.get("id", ""))
    tree = [t for t in tree if str(t.get("id", "")) != eid]
    tree.append(entry)
    CFG.sync_dir.mkdir(parents=True, exist_ok=True)
    with open(CFG.tree_json, "w") as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    return tree


def _ensure_parent_hierarchy(filepath):
    """Create Confluence pages for missing parent directories.

    Walks from base_output_dir down to the file's parent directory.
    For each directory segment that has no corresponding Confluence page
    in tree_cache, creates a placeholder page under the correct parent.
    Updates tree_cache on disk after each creation.

    Returns the page_id that should be the parent for filepath, or None.
    """
    p = Path(filepath).resolve()
    base = CFG.base_output_dir.resolve()
    try:
        rel = p.relative_to(base)
    except ValueError:
        return None

    dir_parts = list(p.parent.relative_to(base).parts)
    if not dir_parts:
        tree = _load_tree_cache() or []
        for item in tree:
            if item.get("depth", -1) == 0:
                return item["id"]
        return None

    tree = _load_tree_cache() or []
    root_item = None
    for item in tree:
        if item.get("depth", -1) == 0:
            root_item = item
            break
    if not root_item:
        return None

    root_prefix = sanitize_filename(root_item.get("title", ""))
    current_parent_id = root_item["id"]

    for depth_idx in range(len(dir_parts)):
        segment = dir_parts[depth_idx]
        candidate_parts = dir_parts[: depth_idx + 1]

        found_id = None
        for item in tree:
            cpath = str(item.get("confluence_path", "")).strip("/")
            if not cpath:
                continue
            item_parts = cpath.split("/")
            item_san = [sanitize_filename(x) for x in item_parts]

            if len(item_san) == depth_idx + 1 and item_san == candidate_parts:
                found_id = item["id"]
                break
            if (len(item_san) == depth_idx + 2
                    and item_san[0] == root_prefix
                    and item_san[1:] == candidate_parts):
                found_id = item["id"]
                break

        if found_id:
            current_parent_id = found_id
            continue

        dir_title = segment
        space_id = _resolve_space_id(CFG, fm={})
        if not space_id:
            print("WARN: Cannot create directory page '%s' - space_id unresolved" % dir_title, flush=True)
            return current_parent_id

        existing = _find_child_page_by_title(current_parent_id, dir_title)
        if existing:
            new_page_id = existing
            print("  Found existing page for directory '%s': %s" % (dir_title, new_page_id), flush=True)
        else:
            index_file = base / Path(*dir_parts[: depth_idx + 1]) / (segment + ".md")
            if index_file.exists():
                print("  Skipping placeholder for '%s' — index file exists: %s" % (dir_title, index_file.name), flush=True)
                continue

            payload = {
                "spaceId": space_id,
                "parentId": current_parent_id,
                "status": "current",
                "title": dir_title,
                "body": {"representation": "storage",
                         "value": "<p>Auto-created directory page by csync</p>"},
            }
            resp = api_post("pages", payload)
            if not isinstance(resp, dict) or not resp.get("id"):
                fallback_id = _find_page_in_space_by_title(dir_title)
                if fallback_id:
                    new_page_id = fallback_id
                    print("  Directory page '%s' already exists in space: %s" % (dir_title, new_page_id), flush=True)
                else:
                    print("WARN: Failed to create directory page '%s'" % dir_title, flush=True)
                    return current_parent_id
            else:
                new_page_id = str(resp["id"])
                print("  Created directory page '%s': %s (parent: %s)" % (dir_title, new_page_id, current_parent_id), flush=True)

        cpath_for_entry = "/".join(candidate_parts)
        entry = {
            "id": new_page_id,
            "title": dir_title,
            "confluence_path": root_item.get("title", "") + "/" + cpath_for_entry,
            "depth": depth_idx + 1,
            "parent_id": current_parent_id,
        }
        tree = _save_tree_cache_entry(entry)
        current_parent_id = new_page_id
        time.sleep(CFG.rate_limit_delay)

    return current_parent_id


def _find_child_page_by_title(parent_id, title):
    """Search for a child page by title under parent_id. Returns page_id or None."""
    try:
        url = "pages/%s/children?limit=250" % parent_id
        resp = api_get(url)
        if not isinstance(resp, dict):
            return None
        results = resp.get("results", [])
        for page in results:
            if str(page.get("title", "")).strip() == title.strip():
                return str(page["id"])
    except Exception:
        pass
    return None


def _find_page_in_space_by_title(title):
    """Search for a page by exact title in the configured space. Returns page_id or None."""
    space_id = _resolve_space_id(CFG, fm={})
    if not space_id:
        return None
    try:
        from urllib.parse import quote
        url = "pages?title=%s&space-id=%s&limit=5" % (quote(title, safe=""), space_id)
        resp = api_get(url)
        if not isinstance(resp, dict):
            return None
        results = resp.get("results", [])
        for page in results:
            if str(page.get("title", "")).strip() == title.strip():
                return str(page["id"])
    except Exception:
        pass
    return None


def _infer_parent_from_path(filepath):
    """Infer Confluence parent page from local file path using tree_cache.

    Walks up the directory hierarchy to find a directory that corresponds to
    a known Confluence page. Returns (parent_page_id, title) or None.
    """
    p = Path(filepath).resolve()
    base = CFG.base_output_dir.resolve()

    try:
        p.relative_to(base)
    except ValueError:
        return None

    title = p.stem

    tree = _load_tree_cache()
    if not tree:
        return None

    root_item = None
    for item in tree:
        if item.get("depth", -1) == 0:
            root_item = item
            break

    parent_dir = p.parent
    try:
        rel_dir = parent_dir.relative_to(base)
    except ValueError:
        return None

    if str(rel_dir) == ".":
        if root_item:
            return (root_item["id"], title)
        return None

    dir_parts = list(rel_dir.parts)

    root_prefix = sanitize_filename(root_item.get("title", "")) if root_item else ""

    for depth in range(len(dir_parts), 0, -1):
        candidate_parts = dir_parts[:depth]

        for item in tree:
            cpath = str(item.get("confluence_path", "")).strip("/")
            if not cpath:
                continue
            item_path_parts = cpath.split("/")
            item_sanitized = [sanitize_filename(x) for x in item_path_parts]

            if len(item_sanitized) == depth and item_sanitized == candidate_parts:
                return (item["id"], title)

            if (len(item_sanitized) == depth + 1
                    and item_sanitized[0] == root_prefix
                    and item_sanitized[1:] == candidate_parts):
                return (item["id"], title)

    if root_item:
        return (root_item["id"], title)

    return None


# --- T402: Change Detection ---


def _detect_local_changes(state):
    """Detect locally modified files using SHA256 content hash comparison.

    Falls back to mtime with a 5-minute threshold for entries without a stored hash.
    Returns list of (page_id, filepath, state_entry) for changed files.
    """
    changed = []
    for page_id, entry in state.get("items", {}).items():
        synced_at = entry.get("synced_at", "")
        if not synced_at:
            continue

        cpath = entry.get("confluence_path", "")
        if not cpath:
            continue

        filepath = confluence_path_to_output_path(cpath)
        if not filepath or not filepath.exists():
            continue

        stored_hash = entry.get("content_hash", "")
        if stored_hash:
            current_hash = _file_content_hash(filepath)
            if current_hash and current_hash != stored_hash:
                changed.append((page_id, filepath, entry))
        else:
            try:
                mtime = os.path.getmtime(filepath)
                synced_ts = datetime.fromisoformat(synced_at).timestamp()
                if mtime - synced_ts > 300:
                    changed.append((page_id, filepath, entry))
            except (OSError, ValueError):
                continue
    return changed


def _detect_renames(tree, state):
    """Detect pages renamed on Confluence and move local files accordingly.

    Compares tree title/path with state to find renames.
    Returns count of renames performed.
    """
    import shutil
    renames = 0

    for item in tree:
        page_id = str(item.get("id", "")).strip()
        if page_id not in state.get("items", {}):
            continue

        old_cpath = state["items"][page_id].get("confluence_path", "")
        new_cpath = str(item.get("confluence_path", "")).strip("/")
        if not old_cpath or not new_cpath or old_cpath == new_cpath:
            continue

        old_filepath = confluence_path_to_output_path(old_cpath)
        new_filepath = confluence_path_to_output_path(new_cpath)

        if not old_filepath or not new_filepath:
            continue
        if not old_filepath.exists():
            continue
        if old_filepath == new_filepath:
            continue

        new_filepath.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(old_filepath), str(new_filepath))
            print("  Renamed: %s -> %s" % (
                old_filepath.relative_to(CFG.base_output_dir),
                new_filepath.relative_to(CFG.base_output_dir)), flush=True)
        except OSError as e:
            print("  WARN: rename failed: %s" % e, flush=True)
            continue

        if new_filepath.exists() and new_filepath.suffix == ".md":
            try:
                txt = new_filepath.read_text(encoding="utf-8")
                fm, body = parse_front_matter(txt)
                if fm.get("title"):
                    fm["title"] = item.get("title", fm["title"])
                    new_text = merge_front_matter_and_body(fm, body)
                    new_filepath.write_text(new_text, encoding="utf-8")
            except Exception:
                pass

        state["items"][page_id]["confluence_path"] = new_cpath
        state["items"][page_id]["title"] = item.get("title", "")
        if new_filepath.exists():
            state["items"][page_id]["content_hash"] = _file_content_hash(new_filepath)
        renames += 1

    return renames


def _detect_new_local_files(state):
    """Detect local .md files not tracked in sync state and without confluence_id.

    Scans base_output_dir for .md files, skips those already tracked
    or having confluence_id in front matter.
    Returns list of Path objects for new files.
    """
    tracked_paths = set()
    for entry in state.get("items", {}).values():
        p = entry.get("confluence_path", "")
        if p:
            tracked_paths.add(p)

    new_files = []
    excl = CFG.exclude_paths + ["_assets", ".csync.yaml"]
    for md_file in CFG.base_output_dir.rglob("*.md"):
        if any(e in str(md_file) for e in excl):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _ = parse_front_matter(text)
        if fm.get("confluence_id"):
            continue
        new_files.append(md_file)
    return new_files


# --- T403: Auto-Push ---


def _auto_push_local_changes(local_changes, remote_changed_ids, dry_run=False):
    """Push locally modified files to Confluence. Skip conflicts (both modified).

    Returns (pushed_count, conflict_ids).
    """
    pushed = 0
    conflicts = set()

    if not local_changes:
        return pushed, conflicts

    print("--- Local Changes Detected ---", flush=True)
    print("Found %d locally modified file(s)" % len(local_changes), flush=True)

    for page_id, filepath, entry in local_changes:
        title = entry.get("title", filepath.name)[:60]

        if page_id in remote_changed_ids:
            conflicts.add(page_id)
            print("  CONFLICT: %s (both local & remote changed, skipping)" % title, flush=True)
            continue

        if dry_run:
            print("  PUSH (dry): %s" % title, flush=True)
            continue

        print("  PUSH: %s" % title, flush=True)
        result = cmd_push(CFG, str(filepath), dry_run=False)
        if result.get("ok"):
            pushed += 1
        else:
            print("  WARN: Push failed for %s: %s" % (title, result.get("message", "")), flush=True)

    print("Pushed: %d, Conflicts: %d" % (pushed, len(conflicts)), flush=True)
    if conflicts:
        print("(Conflicting files left untouched. Resolve manually, then push/pull.)", flush=True)
    print(flush=True)
    return pushed, conflicts


def _prelink_existing_pages(new_files):
    """Match local files to existing Confluence pages using path-based matching.

    Primary: compare each file's expected confluence_path against tree_cache entries.
    Fallback: title-only matching, restricted to titles that appear exactly once
    in tree_cache (unique titles) to avoid mismatches.

    Returns (linked_files, remaining_files).
    """
    tree = _load_tree_cache()
    if not tree:
        return [], new_files

    base = CFG.base_output_dir.resolve()

    root_title = ""
    for item in tree:
        if item.get("depth", -1) == 0:
            root_title = str(item.get("title", "")).strip()
            break

    path_map = {}
    for item in tree:
        cpath = str(item.get("confluence_path", "")).strip("/")
        if cpath and item.get("id"):
            normalized = cpath
            if root_title and cpath.startswith(root_title + "/"):
                normalized = cpath[len(root_title) + 1:]
            path_map[normalized] = item

    title_counts = {}
    for item in tree:
        t = str(item.get("title", "")).strip()
        if t:
            title_counts[t] = title_counts.get(t, 0) + 1
    unique_title_map = {}
    for item in tree:
        t = str(item.get("title", "")).strip()
        if t and item.get("id") and title_counts.get(t, 0) == 1:
            unique_title_map[t] = item

    linked = []
    remaining = []
    for filepath in new_files:
        try:
            text = filepath.read_text(encoding="utf-8")
        except Exception:
            remaining.append(filepath)
            continue

        fm, body = parse_front_matter(text)
        if not isinstance(fm, dict):
            fm = {}

        title = str(fm.get("title") or "").strip() or filepath.stem

        item = None
        match_type = "unique-title"
        try:
            rel = filepath.resolve().relative_to(base)
            is_index = (filepath.stem == filepath.parent.name)
            if is_index:
                expected_path = "/".join(rel.parent.parts)
            else:
                expected_path = "/".join(list(rel.parent.parts) + [rel.stem])
            item = path_map.get(expected_path)
            if item:
                match_type = "path"
        except ValueError:
            pass

        if not item:
            item = unique_title_map.get(title)
            match_type = "unique-title"

        if item:
            page_id = str(item["id"])
            site = str(_cfg_value(CFG, "confluence_base_url", "") or "").rstrip("/")
            page_url = "%s/pages/%s" % (site, page_id) if site else ""
            now = _iso_utc_now()
            updates = {
                "confluence_id": page_id,
                "synced_at": now,
            }
            if page_url:
                updates["confluence_url"] = page_url
            _write_front_matter_updates(filepath, fm, body, updates)
            linked.append(filepath)
            print("  LINKED: %s → page %s (%s match)" % (filepath.name, page_id, match_type), flush=True)
        else:
            remaining.append(filepath)

    if linked:
        print("Pre-linked %d file(s) to existing Confluence pages" % len(linked), flush=True)

    return linked, remaining


def _auto_push_new_files(new_files, dry_run=False):
    """Push new local files (no confluence_id) to Confluence as new pages.

    Sorts files by directory depth (shallow first) and ensures parent
    directory pages exist on Confluence before creating child pages.
    Returns count of successfully pushed files.
    """
    if not new_files:
        return 0

    linked, new_files = _prelink_existing_pages(new_files)

    if not new_files:
        print("All %d file(s) linked to existing pages. Nothing new to push." % len(linked), flush=True)
        return 0

    base = CFG.base_output_dir.resolve()

    def _sort_key(f):
        resolved = f.resolve()
        rel = resolved.relative_to(base)
        depth = len(rel.parts)
        is_index = 0 if (f.stem == f.parent.name) else 1
        return (depth, is_index, str(f).lower())

    new_files = sorted(new_files, key=_sort_key)

    print("--- New Local Files Detected (hierarchical bootstrap) ---", flush=True)
    print("Found %d new local file(s) to create on Confluence" % len(new_files), flush=True)

    pushed = 0
    for filepath in new_files:
        rel = filepath.relative_to(CFG.base_output_dir)

        if not dry_run:
            parent_id = _ensure_parent_hierarchy(filepath)
            if parent_id:
                text = filepath.read_text(encoding="utf-8")
                fm, body = parse_front_matter(text)
                if isinstance(fm, dict) and not fm.get("confluence_parent"):
                    fm["confluence_parent"] = parent_id
                    _write_front_matter_updates(filepath, fm, body, {"confluence_parent": parent_id})

        if dry_run:
            print("  CREATE (dry): %s" % rel, flush=True)
            continue

        print("  CREATE: %s" % rel, flush=True)
        result = cmd_push_new(CFG, str(filepath), dry_run=False)
        if result.get("ok"):
            pushed += 1
            page_id = result.get("page_id", "")
            if page_id:
                is_index = (filepath.stem == filepath.parent.name)
                if is_index:
                    cpath_parts = list(rel.parent.parts)
                else:
                    cpath_parts = list(rel.parent.parts) + [rel.stem]
                tree = _load_tree_cache() or []
                root_title = ""
                for item in tree:
                    if item.get("depth", -1) == 0:
                        root_title = item.get("title", "")
                        break
                cpath = root_title + "/" + "/".join(cpath_parts) if root_title else "/".join(cpath_parts)
                actual_title = result.get("title", rel.stem)
                entry = {
                    "id": page_id,
                    "title": actual_title,
                    "confluence_path": cpath,
                    "depth": len(cpath_parts),
                    "parent_id": parent_id if parent_id else "",
                }
                _save_tree_cache_entry(entry)
        else:
            print("  WARN: Create failed for %s: %s" % (rel, result.get("message", "")), flush=True)

    print("Created: %d" % pushed, flush=True)
    print(flush=True)
    return pushed


# --- T404: cmd_sync ---


def cmd_sync(force=False, dry_run=False, refresh=False, no_push=False):
    """Bidirectional sync: push local changes, then pull remote changes."""

    # Load or refresh tree
    if refresh or not CFG.tree_json.exists():
        if not CFG.tree_json.exists():
            print("No tree cache found. Running crawl...", flush=True)
        tree = _crawl_and_save()
    else:
        tree = _load_tree_cache()
        if tree is None:
            print("Tree cache corrupted. Running crawl...", flush=True)
            tree = _crawl_and_save()

    state = load_sync_state()
    now = datetime.now().isoformat()

    # Phase 0: Detect renames
    rename_count = _detect_renames(tree, state)
    if rename_count > 0:
        print("Renames detected: %d" % rename_count, flush=True)
        save_sync_state(state)

    # Phase 1: Detect local changes (fast, local-only)
    local_changes = [] if no_push else _detect_local_changes(state)

    new_items = []
    updated_items = []
    unchanged = 0
    errors = 0

    print("=== Bidirectional Sync [%s] ===" % CFG.label, flush=True)
    print("Started: " + now, flush=True)
    print("Total pages: %d" % len(tree), flush=True)
    if local_changes:
        print("Local modifications: %d" % len(local_changes), flush=True)
    print("Checking remote changes...", flush=True)
    print(flush=True)

    # Phase 2: Check remote changes via version_number
    for idx, item in enumerate(tree):
        page_id = str(item.get("id", "")).strip()

        if page_id not in state["items"]:
            new_items.append(item)
            continue

        if force:
            updated_items.append(item)
            continue

        vinfo = _get_page_version_info(page_id)
        if vinfo:
            stored_ver = state["items"][page_id].get("version_number", 0)
            remote_ver = vinfo.get("version_number", 0)
            if remote_ver != stored_ver:
                updated_items.append(item)
                print("[%d/%d] Changed: %s (v%d->v%d)" % (
                    idx + 1, len(tree),
                    item.get("title", "")[:60],
                    stored_ver, remote_ver), flush=True)
            else:
                unchanged += 1
        else:
            updated_items.append(item)

        time.sleep(CFG.rate_limit_delay)
        if (idx + 1) % 50 == 0:
            print("  ... checked %d/%d" % (idx + 1, len(tree)), flush=True)

    print(flush=True)

    # Phase 3: Push local changes before downloading remote
    remote_changed_ids = set(str(i.get("id", "")) for i in updated_items)
    conflict_ids = set()
    if local_changes:
        pushed_count, conflict_ids = _auto_push_local_changes(
            local_changes, remote_changed_ids, dry_run=dry_run
        )
        if conflict_ids:
            updated_items = [i for i in updated_items if str(i.get("id", "")) not in conflict_ids]

    # Phase 3.5: Push new local files
    if not no_push:
        new_local_files = _detect_new_local_files(state)
        if new_local_files:
            _auto_push_new_files(new_local_files, dry_run=dry_run)

    print("New: %d, Updated: %d, Unchanged: %d" % (len(new_items), len(updated_items), unchanged), flush=True)
    if conflict_ids:
        print("Conflicts (skipped): %d" % len(conflict_ids), flush=True)

    # Phase 4: Download remote changes
    to_download = new_items + updated_items
    if not to_download:
        print("Nothing to sync from Confluence.", flush=True)
        state["last_sync"] = now
        save_sync_state(state)
        return

    if dry_run:
        print(flush=True)
        print("Would download %d page(s):" % len(to_download), flush=True)
        for idx, item in enumerate(to_download):
            label = "NEW" if item in new_items else "UPD"
            print("  [%d] %s %s" % (idx + 1, label, item.get("title", "")[:60]), flush=True)
        print("\n(dry-run: no files modified)", flush=True)
        return

    print(flush=True)
    print("Downloading %d page(s)..." % len(to_download), flush=True)

    success = 0
    for idx, item in enumerate(to_download):
        page_id = str(item.get("id", "")).strip()
        label = "NEW" if item in new_items else "UPD"
        cpath = str(item.get("confluence_path", "")).strip("/")
        out_path = confluence_path_to_output_path(cpath)
        print("[%d/%d] %s %s" % (
            idx + 1, len(to_download), label,
            item.get("title", "")[:60]), flush=True)

        r = cmd_pull_page_to_path(page_id, out_path, dry_run=False)
        if r.get("ok"):
            success += 1
        else:
            errors += 1
            print("  FAIL: %s" % r.get("message", ""), flush=True)

        vinfo = _get_page_version_info(page_id)
        entry = {
            "title": item.get("title", ""),
            "confluence_path": cpath,
            "version_number": vinfo["version_number"] if vinfo else 0,
            "updated_at": vinfo["updated_at"] if vinfo else "",
            "synced_at": datetime.now().isoformat(),
        }
        if out_path and out_path.exists():
            entry["content_hash"] = _file_content_hash(out_path)
        state["items"][page_id] = entry
        save_sync_state(state)
        time.sleep(CFG.rate_limit_delay)

    state["last_sync"] = now
    save_sync_state(state)

    print(flush=True)
    print("=== Sync Complete ===", flush=True)
    print("Success: %d, Errors: %d (New: %d, Updated: %d)" % (
        success, errors, len(new_items), len(updated_items)), flush=True)
    if conflict_ids:
        print("Conflicts: %d (resolve manually)" % len(conflict_ids), flush=True)
    print("Finished: " + datetime.now().isoformat(), flush=True)


# --- T405: cmd_init_state / cmd_status ---


def cmd_init_state():
    """Initialize sync state from existing local files + tree cache."""
    tree = _load_tree_cache()
    if tree is None:
        print("No tree cache found. Running crawl first...", flush=True)
        tree = _crawl_and_save()

    state = {"items": {}, "last_sync": None}
    found = 0
    missing = 0

    for item in tree:
        page_id = str(item.get("id", "")).strip()
        cpath = str(item.get("confluence_path", "")).strip("/")
        if not page_id or not cpath:
            missing += 1
            continue

        filepath = confluence_path_to_output_path(cpath)
        if filepath and filepath.exists():
            entry = {
                "title": item.get("title", ""),
                "confluence_path": cpath,
                "version_number": 0,
                "updated_at": "",
                "synced_at": datetime.now().isoformat(),
                "content_hash": _file_content_hash(filepath),
            }
            state["items"][page_id] = entry
            found += 1
        else:
            missing += 1

    state["last_sync"] = datetime.now().isoformat()
    save_sync_state(state)
    print("Initialized: %d items tracked, %d missing/not-pulled" % (found, missing), flush=True)


def cmd_status():
    """Show sync status summary with local change detection."""
    state = load_sync_state()
    items = state.get("items", {})
    last_sync = state.get("last_sync", "never")

    print("=== Sync Status [%s] ===" % CFG.label, flush=True)
    print("Root: %s" % CFG.root_page_id, flush=True)
    print("Output: %s" % CFG.base_output_dir, flush=True)
    print("Tracked items: %d" % len(items), flush=True)
    print("Last sync: %s" % last_sync, flush=True)

    if not items:
        print("\n(No items tracked. Run 'init-state' or 'sync' first.)", flush=True)
        return

    local_changes = _detect_local_changes(state)
    if local_changes:
        print("\nLocal modifications: %d" % len(local_changes), flush=True)
        for page_id, filepath, entry in local_changes:
            rel = filepath.relative_to(CFG.base_output_dir)
            print("  M %s" % rel, flush=True)
    else:
        print("\nNo local modifications detected.", flush=True)

    new_files = _detect_new_local_files(state)
    if new_files:
        print("\nNew local files (untracked): %d" % len(new_files), flush=True)
        for fp in new_files[:10]:
            print("  ? %s" % fp.relative_to(CFG.base_output_dir), flush=True)
        if len(new_files) > 10:
            print("  ... and %d more" % (len(new_files) - 10), flush=True)


# ==========================================
# Init: Create new workspace
# ==========================================

def _find_skill_dir():
    """Discover the csync skill directory (contains .env and scripts/)."""
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir.parent
    if (candidate / "SKILL.md").exists() or (candidate / "scripts").is_dir():
        return candidate
    return None


def _load_tokens_from_env_files():
    """Load Confluence credentials from .env files with fallback chain."""
    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    base_url = os.environ.get("CONFLUENCE_BASE_URL", "")
    if email and token:
        return base_url, email, token

    skill_dir = _find_skill_dir()
    if skill_dir:
        env_path = skill_dir / ".env"
        if env_path.exists():
            env_data = {}
            for line in env_path.read_text().split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env_data[k.strip()] = v.strip()
            email = email or env_data.get("CONFLUENCE_EMAIL", "")
            token = token or env_data.get("CONFLUENCE_API_TOKEN", "")
            base_url = base_url or env_data.get("CONFLUENCE_BASE_URL", "")
    return base_url, email, token


def _fetch_page_title(page_id, base_url, email, api_token):
    """Confluence API v2 でページタイトルを取得（init 用）"""
    if not email or not api_token or not base_url:
        return ""
    credentials = base64.b64encode(("%s:%s" % (email, api_token)).encode()).decode()
    headers = {
        "Authorization": "Basic " + credentials,
        "Accept": "application/json",
    }
    url = "%s/api/v2/pages/%s" % (base_url.rstrip("/"), page_id)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("title", "")
    except Exception:
        pass
    return ""


def cmd_init_workspace(confluence_url, output_dir):
    page_id = extract_page_id_from_url(confluence_url)
    if not page_id:
        print("ERROR: Could not extract page ID from URL: %s" % confluence_url, flush=True)
        sys.exit(1)

    detected_base_url = extract_base_url_from_url(confluence_url)

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sync_dir = out / "_csync"
    sync_dir.mkdir(exist_ok=True)

    env_base_url, env_email, env_token = _load_tokens_from_env_files()
    base_url = detected_base_url or env_base_url

    page_title = _fetch_page_title(page_id, base_url, env_email, env_token)
    label = page_title if page_title else out.name
    if page_title:
        print("Confluence page title: %s" % page_title, flush=True)

    config_path = out / ".csync.yaml"
    config_content = """root_page_id: "%s"
confluence_base_url: "%s"
label: "%s"
crawl_max_depth: 10
rate_limit_delay: 0.35
exclude_paths:
  - "_csync"
  - "_nsync"
  - "_archived"
  - "_sync"
  - "_backup"
""" % (page_id, base_url, label)
    config_path.write_text(config_content, encoding="utf-8")
    print("Created: %s" % config_path, flush=True)

    env_path = sync_dir / ".env"
    if not env_path.exists():
        skill_dir = _find_skill_dir()
        shared_env = skill_dir / ".env" if skill_dir else None
        if shared_env and shared_env.exists():
            import shutil
            shutil.copy2(str(shared_env), str(env_path))
            print("Copied credentials from: %s" % shared_env, flush=True)
        else:
            env_content = "CONFLUENCE_BASE_URL=%s\nCONFLUENCE_EMAIL=\nCONFLUENCE_API_TOKEN=\n" % base_url
            env_path.write_text(env_content, encoding="utf-8")
            print("Created: %s  (fill in your credentials!)" % env_path, flush=True)
    else:
        print("Exists: %s" % env_path, flush=True)

    wrapper_path = out / "csync.sh"
    csync_py_abs = str(Path(__file__).resolve())
    wrapper_content = """#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
CSYNC="${CSYNC_SCRIPT:-__CSYNC_PY_ABS__}"
if [ ! -f "$CSYNC" ]; then
  GIT_ROOT="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null)"
  CSYNC=""
  for candidate in \\
    "${GIT_ROOT:-.}/.claude/skills/csync/scripts/csync.py" \\
    "$HOME/.claude/skills/csync/scripts/csync.py"; do
    [ -f "$candidate" ] && CSYNC="$candidate" && break
  done
fi
if [ -z "$CSYNC" ]; then
  echo "ERROR: csync.py not found. Set CSYNC_SCRIPT or install to .claude/skills/csync/"
  exit 1
fi
SKILL_DIR="$(dirname "$(dirname "$CSYNC")")"
if [ -f "$DIR/_csync/.env" ]; then set -a; . "$DIR/_csync/.env"; set +a; fi
if [ -z "$CONFLUENCE_API_TOKEN" ] && [ -f "$SKILL_DIR/.env" ]; then set -a; . "$SKILL_DIR/.env"; set +a; fi
[ -z "$CONFLUENCE_API_TOKEN" ] && echo "ERROR: CONFLUENCE_API_TOKEN not set." && exit 1
[ -z "$CONFLUENCE_EMAIL" ] && echo "ERROR: CONFLUENCE_EMAIL not set." && exit 1
python3 "$CSYNC" --config "$DIR/.csync.yaml" "${@:-sync}"
""".replace("__CSYNC_PY_ABS__", csync_py_abs)
    wrapper_path.write_text(wrapper_content, encoding="utf-8")
    os.chmod(str(wrapper_path), 0o755)
    print("Created: %s" % wrapper_path, flush=True)

    print("\n=== csync workspace initialized ===", flush=True)
    print("Root page: %s" % page_id, flush=True)
    print("Base URL: %s" % base_url, flush=True)
    print("Output: %s" % out, flush=True)
    print("\nNext steps:", flush=True)
    print("  1. Edit %s and set CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN" % env_path, flush=True)
    print("  2. cd %s" % out, flush=True)
    print("  3. ./csync.sh sync", flush=True)


# ==========================================
# Main
# ==========================================

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h"):
        print_usage()
        return

    if args[0] == "--version":
        print("csync %s" % __version__)
        return

    config_path = None
    if args[0] == "--config" and len(args) >= 2:
        config_path = args[1]
        args = args[2:]

    command = args[0] if args else "sync"

    if command in ("--help", "-h"):
        print_usage()
        return

    if command == "init":
        if len(args) < 2:
            print("Usage: csync.py init <confluence-url> [output-dir]", flush=True)
            sys.exit(1)
        confluence_url = args[1]
        output_dir = args[2] if len(args) >= 3 else "."
        cmd_init_workspace(confluence_url, output_dir)
        return

    if not config_path:
        for candidate in [".csync.yaml", "_csync/.csync.yaml", "_sync/.csync.yaml"]:
            if Path(candidate).exists():
                config_path = candidate
                break
        if not config_path:
            print("ERROR: No .csync.yaml found. Run 'csync.py init <url> <dir>' first.", flush=True)
            sys.exit(1)

    load_config(config_path)

    # Load credentials from .env files before init_api
    env_base_url, env_email, env_token = _load_tokens_from_env_files()
    if env_base_url and not CFG.confluence_base_url:
        CFG.confluence_base_url = env_base_url
    if env_email:
        os.environ.setdefault("CONFLUENCE_EMAIL", env_email)
    if env_token:
        os.environ.setdefault("CONFLUENCE_API_TOKEN", env_token)

    init_api()

    LOCAL_ONLY_CMDS = ("status", "init-state")
    if command not in LOCAL_ONLY_CMDS and not (EMAIL and API_TOKEN):
        print("ERROR: CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN not set", flush=True)
        sys.exit(1)

    # --- SG2: Pull (single + recursive) ---
    if command == "pull":
        recursive, dry_run, pull_tail = _parse_pull_cli_args(args[1:])
        if len(pull_tail) < 1:
            print(
                "Usage: csync.py pull [--dry-run] [--recursive|-r] <url|page-id|file.md> [out.md]",
                flush=True,
            )
            sys.exit(1)
        target = pull_tail[0]
        out_override = pull_tail[1] if len(pull_tail) >= 2 else None

        if recursive:
            rid = extract_page_id_from_url(target)
            if not rid and str(target).strip().isdigit():
                rid = str(target).strip()
            if not rid:
                print("ERROR: could not resolve root page id from: %s" % target, flush=True)
                sys.exit(1)
            summ = cmd_pull_recursive(rid, dry_run=dry_run)
            print(
                "pull -r: OK=%d FAIL=%d SKIP=%d" % (summ["ok"], summ["fail"], summ["skip"]),
                flush=True,
            )
            for fitem in summ.get("failures") or []:
                print("  FAIL %s: %s" % (fitem.get("id"), fitem.get("error")), flush=True)
            sys.exit(1 if summ["fail"] else 0)

        page_id = ""
        out_path = None
        if target.lower().endswith(".md"):
            md_path = Path(target)
            if not md_path.is_file():
                print("ERROR: markdown file not found: %s" % target, flush=True)
                sys.exit(1)
            text = md_path.read_text(encoding="utf-8")
            fm, _body = parse_front_matter(text)
            page_id = str(fm.get("confluence_id") or "").strip()
            if not page_id:
                print("ERROR: missing confluence_id in front matter of %s" % target, flush=True)
                sys.exit(1)
            out_path = md_path.resolve()
        else:
            page_id = extract_page_id_from_url(target) or ""
            if not page_id and str(target).strip().isdigit():
                page_id = str(target).strip()

        if not page_id:
            print("ERROR: could not resolve page id", flush=True)
            sys.exit(1)

        if out_path is None:
            pj = fetch_page_with_storage(page_id)
            if not isinstance(pj, dict):
                print("ERROR: failed to fetch page %s" % page_id, flush=True)
                sys.exit(1)
            if out_override:
                out_path = Path(out_override).resolve()
            else:
                title = sanitize_filename(pj.get("title") or ("page-%s" % page_id))
                out_path = (CFG.base_output_dir / (title + ".md")).resolve()

        r = cmd_pull_page_to_path(page_id, out_path, dry_run=dry_run)
        if not r.get("ok"):
            print("ERROR: %s" % r.get("message"), flush=True)
            sys.exit(1)
        print("Pulled page %s -> %s" % (page_id, out_path), flush=True)
        return

    # --- SG3: Push commands ---
    elif command == "push":
        dry_run = "--dry-run" in args[1:]
        recursive = "--recursive" in args[1:] or "-r" in args[1:]
        push_args = [a for a in args[1:] if a not in ("--dry-run", "--recursive", "-r")]
        if len(push_args) < 1:
            print("Usage: csync.py push [--dry-run] [--recursive|-r] <filepath.md>", flush=True)
            sys.exit(1)
        filepath = push_args[0]
        if recursive:
            summary = cmd_push_recursive(CFG, filepath, dry_run=dry_run)
            print(
                "push -r: OK=%d FAIL=%d SKIP=%d"
                % (summary.get("ok", 0), summary.get("fail", 0), summary.get("skip", 0)),
                flush=True,
            )
            if summary.get("fail", 0):
                sys.exit(1)
            return
        result = cmd_push(CFG, filepath, dry_run=dry_run)
        if not result.get("ok"):
            print("ERROR: %s" % result.get("message", "push failed"), flush=True)
            sys.exit(1)
        return

    elif command == "new":
        new_args = [a for a in args[1:] if not a.startswith("--")]
        if len(new_args) < 2:
            print('Usage: csync.py new <parent-url-or-title> "Page Title"', flush=True)
            sys.exit(1)
        result = cmd_new(CFG, new_args[0], new_args[1])
        if not result.get("ok"):
            print("ERROR: %s" % result.get("message", "new failed"), flush=True)
            sys.exit(1)
        return

    # --- SG4: Sync commands ---
    elif command == "sync":
        dry_run = "--dry-run" in args[1:]
        force = "--force" in args[1:]
        refresh_flag = "--refresh" in args[1:]
        no_push = "--no-push" in args[1:]
        full = "--full" in args[1:]
        if full:
            force = True
            refresh_flag = True
        cmd_sync(force=force, dry_run=dry_run, refresh=refresh_flag, no_push=no_push)

    elif command == "status":
        cmd_status()

    elif command == "init-state":
        cmd_init_state()

    elif command == "full":
        cmd_sync(force=True, dry_run=False, refresh=True, no_push=False)

    else:
        print("Unknown command: %s" % command, flush=True)
        print_usage()
        sys.exit(1)


def print_usage():
    print("csync %s - Confluence Sync Tool" % __version__, flush=True)
    print("", flush=True)
    print("Usage:", flush=True)
    print("  csync.py init <confluence-url> [output-dir]    Setup new workspace", flush=True)
    print("  csync.py --config <.csync.yaml> <command>      Run with config", flush=True)
    print("", flush=True)
    print("Commands:", flush=True)
    print("  sync               Bidirectional sync (push local changes, pull remote)", flush=True)
    print("  sync --refresh     Refresh page list, then sync", flush=True)
    print("  sync --force       Force re-download all pages", flush=True)
    print("  sync --full        Refresh + force (complete re-sync)", flush=True)
    print("  sync --dry-run     Show what would be synced", flush=True)
    print("  sync --no-push     Skip local change detection & auto-push", flush=True)
    print("  pull <file>        Pull single file from Confluence (.md or URL)", flush=True)
    print("  pull -r <url>      Recursively pull a subtree by Confluence URL", flush=True)
    print("  push <file>        Push local file to Confluence", flush=True)
    print("  push -r <file>     Push recursively (create child pages too)", flush=True)
    print('  new <parent> "Title"  Scaffold new page', flush=True)
    print("  status             Show sync status", flush=True)
    print("  init-state         Init state from existing files", flush=True)


def _print_api_stats():
    s = _api_stats
    if s["calls"] == 0:
        return
    parts = ["API: %d calls" % s["calls"]]
    if s["rate_limits"]:
        parts.append("%d rate-limited" % s["rate_limits"])
    if s["errors"]:
        parts.append("%d errors" % s["errors"])
    if s["skipped"]:
        parts.append("%d skipped" % s["skipped"])
    print("[%s]" % ", ".join(parts), flush=True)


if __name__ == "__main__":
    try:
        main()
    except RateLimitExhausted as e:
        print("\nFATAL: %s" % e, flush=True)
        print("Checkpoint saved. Re-run the same command to resume.", flush=True)
        sys.exit(2)
    finally:
        _print_api_stats()
