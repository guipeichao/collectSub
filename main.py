import base64
import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests
import yaml
from loguru import logger
from tqdm import tqdm

from merge_clash import write_merged_config
from pre_check import pre_check


CATEGORY_KEYS = ("机场订阅", "clash订阅", "v2订阅")
STATE_PATH = os.path.join("sub", "state.yaml")
MAX_FAILURES = 3
MAX_WORKERS = 16
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
}
SUBSCRIPTION_HEADERS = {"User-Agent": "ClashforWindows/0.18.1"}
URL_RE = re.compile(r"https?://[^\s<>\"']+")
NODE_SCHEMES = ("ss://", "ssr://", "vmess://", "vless://", "trojan://", "hysteria://", "hysteria2://", "tuic://")
TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "tgclid"}


def load_yaml(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
        return value if value is not None else default
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("读取 YAML 失败 {}: {}", path, exc)
        return default


def yaml_check(path_yaml):
    data = load_yaml(path_yaml, {})
    result = {key: [] for key in CATEGORY_KEYS}
    if isinstance(data, dict):
        for key in CATEGORY_KEYS:
            values = data.get(key, [])
            if isinstance(values, list):
                result[key] = [url for url in values if isinstance(url, str)]
    logger.info("读取结果文件成功：{}", path_yaml)
    return result


def get_config():
    data = load_yaml("config.yaml", {})
    channels = data.get("tgchannel", []) if isinstance(data, dict) else []
    result = []
    seen = set()
    for channel in channels:
        if not isinstance(channel, str):
            continue
        name = channel.rstrip("/").split("/")[-1].strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(f"https://t.me/s/{name}")
    return result


def normalize_url(raw_url):
    value = html.unescape(raw_url).strip().strip(".,;:!?)]}>")
    if not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    query_parts = []
    for item in parts.query.split("&") if parts.query else []:
        key = item.split("=", 1)[0].lower()
        if key in TRACKING_PARAMS or key.startswith("utm_"):
            continue
        query_parts.append(item)
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, "&".join(query_parts), ""))


def get_channel_http(channel_url):
    for attempt in range(1, 4):
        try:
            response = requests.get(channel_url, headers=REQUEST_HEADERS, timeout=(5, 20))
            response.raise_for_status()
            urls = []
            seen = set()
            for match in URL_RE.findall(response.text):
                url = normalize_url(match)
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
            logger.info("{} 获取成功：候选链接 {} 条", channel_url, len(urls))
            return urls
        except requests.RequestException as exc:
            if attempt == 3:
                logger.warning("{} 获取失败：{}", channel_url, exc)
            else:
                logger.debug("{} 第 {} 次重试：{}", channel_url, attempt, exc)
                time.sleep(1.5 ** attempt)
    return []


def contains_node_uri(text):
    lowered = text.lower()
    return any(scheme in lowered for scheme in NODE_SCHEMES)


def decode_subscription_text(text):
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return ""
    try:
        padded = compact + "=" * (-len(compact) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
    except (ValueError, TypeError):
        return ""


def classify_response(response):
    if response.status_code != 200 or not response.content:
        return None
    text = response.text.strip()
    if "subscription-userinfo" in {key.lower() for key in response.headers}:
        return "机场订阅"
    try:
        document = yaml.safe_load(text)
        if isinstance(document, dict) and isinstance(document.get("proxies"), list) and document["proxies"]:
            return "clash订阅"
    except yaml.YAMLError:
        pass
    decoded = decode_subscription_text(text)
    if contains_node_uri(text) or contains_node_uri(decoded):
        return "v2订阅"
    return None


def check_subscription(url):
    last_error = "未知错误"
    for attempt in range(1, 3):
        try:
            response = requests.get(url, headers=SUBSCRIPTION_HEADERS, timeout=(5, 15), allow_redirects=True)
            category = classify_response(response)
            if category:
                return {"url": url, "category": category, "ok": True, "error": ""}
            last_error = f"无有效订阅内容（HTTP {response.status_code}）"
        except requests.RequestException as exc:
            last_error = str(exc)[:200]
        if attempt == 1:
            time.sleep(1.5 ** attempt)
            continue
    return {"url": url, "category": None, "ok": False, "error": last_error}


def load_state():
    data = load_yaml(STATE_PATH, {})
    urls = data.get("urls", {}) if isinstance(data, dict) else {}
    return urls if isinstance(urls, dict) else {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        yaml.safe_dump({"urls": state}, handle, allow_unicode=True, sort_keys=True)


def update_state(state, result, sources):
    url = result["url"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = state.get(url, {}) if isinstance(state.get(url), dict) else {}
    try:
        previous_failures = int(previous.get("failures", 0))
    except (TypeError, ValueError):
        previous_failures = 0
    failures = 0 if result["ok"] else previous_failures + 1
    record = {
        "category": result["category"] or previous.get("category", ""),
        "first_seen": previous.get("first_seen", now),
        "last_checked": now,
        "last_ok": now if result["ok"] else previous.get("last_ok", ""),
        "failures": failures,
        "last_error": result["error"],
        "sources": sorted(set(previous.get("sources", [])) | set(sources.get(url, []))),
    }
    state[url] = record
    return record


def seed_state_from_output(state, output):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for category in CATEGORY_KEYS:
        for url in output.get(category, []):
            state.setdefault(url, {
                "category": category,
                "first_seen": now,
                "last_checked": "",
                "last_ok": "",
                "failures": 0,
                "last_error": "",
                "sources": [],
            })


def main():
    path_yaml = pre_check()
    current = yaml_check(path_yaml)
    channels = get_config()
    logger.info("读取频道配置成功：{} 个唯一频道", len(channels))

    candidates = {}
    for channel_url in channels:
        for url in get_channel_http(channel_url):
            candidates.setdefault(url, []).append(channel_url)

    state = load_state()
    seed_state_from_output(state, current)
    urls_to_check = sorted(set(candidates) | set(state))
    logger.info("开始验证：{} 条唯一订阅链接", len(urls_to_check))
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_subscription, url): url for url in urls_to_check}
        for future in tqdm(as_completed(futures), total=len(futures), desc="订阅筛选"):
            results.append(future.result())

    output = {key: [] for key in CATEGORY_KEYS}
    quarantine = []
    stats = {"ok": 0, "failed": 0, "quarantine": 0}
    for result in results:
        record = update_state(state, result, candidates)
        if result["ok"]:
            stats["ok"] += 1
        else:
            stats["failed"] += 1
        if record["failures"] >= MAX_FAILURES:
            quarantine.append(result["url"])
            stats["quarantine"] += 1
            continue
        category = record.get("category")
        if category in output:
            output[category].append(result["url"])

    for key in output:
        output[key] = sorted(set(output[key]))
    output["失败隔离"] = sorted(set(quarantine))
    with open(path_yaml, "w", encoding="utf-8") as handle:
        yaml.safe_dump(output, handle, allow_unicode=True, sort_keys=False)
    active_urls = [url for key in CATEGORY_KEYS for url in output[key]]
    merged_count = write_merged_config(active_urls)
    save_state(state)
    logger.info("验证完成：有效 {}，失败 {}，隔离 {}，合并节点 {}", stats["ok"], stats["failed"], stats["quarantine"], merged_count)


if __name__ == "__main__":
    main()
