import base64
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, unquote, urlsplit

import requests
import yaml
from loguru import logger


MERGED_PATH = "sub/merged.yaml"
FETCH_HEADERS = {"User-Agent": "ClashforWindows/0.18.1"}
NODE_LINE_RE = re.compile(r"^(?:ss|ssr|vmess|vless|trojan|hysteria|hysteria2|tuic)://", re.IGNORECASE)


def decode_base64_text(value):
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 8:
        return ""
    try:
        padded = compact + "=" * (-len(compact) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
    except (ValueError, TypeError):
        return ""


def as_port(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_vmess(line):
    payload = decode_base64_text(line[8:])
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    port = as_port(data.get("port"))
    if not data.get("add") or not data.get("id") or not port:
        return None
    proxy = {
        "name": data.get("ps") or f"vmess-{data['add']}:{port}",
        "type": "vmess",
        "server": data["add"],
        "port": port,
        "uuid": data["id"],
        "alterId": as_port(data.get("aid", 0)) or 0,
        "cipher": data.get("scy") or "auto",
        "tls": str(data.get("tls", "")).lower() == "tls",
    }
    network = data.get("net") or "tcp"
    if network in {"ws", "grpc", "http", "h2"}:
        proxy["network"] = network
    if network == "ws":
        proxy["ws-opts"] = {"path": data.get("path") or "/", "headers": {"Host": data.get("host", "")}}
    if data.get("sni"):
        proxy["servername"] = data["sni"]
    return proxy


def parse_vless_or_trojan(line):
    try:
        parts = urlsplit(line)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not hostname or not port:
        return None
    query = parse_qs(parts.query)
    scheme = parts.scheme.lower()
    proxy = {
        "name": unquote(parts.fragment) or f"{scheme}-{hostname}:{port}",
        "type": scheme,
        "server": hostname,
        "port": port,
    }
    if scheme == "vless":
        if not parts.username:
            return None
        proxy["uuid"] = unquote(parts.username)
        if query.get("flow"):
            proxy["flow"] = query["flow"][0]
    else:
        if not parts.username:
            return None
        proxy["password"] = unquote(parts.username)
    security = query.get("security", [""])[0]
    if security in {"tls", "reality"} or query.get("sni"):
        proxy["tls"] = True
    if query.get("sni"):
        proxy["servername"] = query["sni"][0]
    network = query.get("type", ["tcp"])[0]
    if network in {"ws", "grpc", "http", "h2"}:
        proxy["network"] = network
    if network == "ws":
        proxy["ws-opts"] = {
            "path": query.get("path", ["/"])[0],
            "headers": {"Host": query.get("host", [""])[0]},
        }
    if network == "grpc" and query.get("serviceName"):
        proxy["grpc-opts"] = {"grpc-service-name": query["serviceName"][0]}
    if query.get("allowInsecure", ["0"])[0] in {"1", "true"}:
        proxy["skip-cert-verify"] = True
    return proxy


def parse_ss(line):
    parts = urlsplit(line)
    payload = line[5:]
    fragment = unquote(parts.fragment)
    if "#" in payload:
        payload = payload.split("#", 1)[0]
    if "@" not in payload:
        payload = decode_base64_text(payload)
    if "@" not in payload:
        return None
    credentials, endpoint = payload.rsplit("@", 1)
    if ":" not in credentials:
        credentials = decode_base64_text(credentials)
    if ":" not in credentials or ":" not in endpoint:
        return None
    method, password = credentials.split(":", 1)
    server, port_text = endpoint.rsplit(":", 1)
    port = as_port(port_text)
    if not method or not password or not server or not port:
        return None
    return {
        "name": fragment or f"ss-{server}:{port}",
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": method,
        "password": password,
    }


def parse_node_line(line):
    line = line.strip()
    if not NODE_LINE_RE.match(line):
        return None
    scheme = line.split(":", 1)[0].lower()
    if scheme == "vmess":
        return parse_vmess(line)
    if scheme in {"vless", "trojan"}:
        return parse_vless_or_trojan(line)
    if scheme == "ss":
        return parse_ss(line)
    return None


def parse_subscription_text(text):
    documents = [text]
    decoded = decode_base64_text(text)
    if decoded and decoded != text:
        documents.append(decoded)
    for document in documents:
        try:
            data = yaml.safe_load(document)
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("proxies"), list):
            return [item for item in data["proxies"] if isinstance(item, dict) and item.get("server") and item.get("type")]
        nodes = []
        for line in document.splitlines():
            node = parse_node_line(line)
            if node:
                nodes.append(node)
        if nodes:
            return nodes
    return []


def fetch_proxies(url):
    try:
        response = requests.get(url, headers=FETCH_HEADERS, timeout=(5, 20), allow_redirects=True)
        if response.status_code != 200:
            return []
        return parse_subscription_text(response.text)
    except requests.RequestException as exc:
        logger.debug("合并订阅失败 {}: {}", url, exc)
        return []
    except Exception as exc:
        logger.warning("解析订阅失败 {}: {}", url, exc)
        return []


def proxy_key(proxy):
    return json.dumps({key: value for key, value in proxy.items() if key != "name"}, sort_keys=True, ensure_ascii=False)


def unique_proxies(proxies):
    result = []
    seen = set()
    names = set()
    for proxy in proxies:
        key = proxy_key(proxy)
        if key in seen:
            continue
        seen.add(key)
        base_name = str(proxy.get("name") or f"节点-{len(result) + 1}")[:80]
        name = base_name
        suffix = 2
        while name in names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        proxy["name"] = name
        names.add(name)
        result.append(proxy)
    return result


def write_merged_config(urls, path=MERGED_PATH):
    proxies = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(fetch_proxies, url) for url in sorted(set(urls))]
        for future in as_completed(futures):
            proxies.extend(future.result())
    proxies = unique_proxies(proxies)
    if not proxies and os.path.exists(path):
        logger.warning("本次没有解析出节点，保留已有汇总文件：{}", path)
        return 0
    names = [proxy["name"] for proxy in proxies]
    selectable = names + ["DIRECT"]
    config = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": [
            {"name": "自动选择", "type": "url-test", "url": "http://www.gstatic.com/generate_204", "interval": 300, "proxies": selectable},
            {"name": "手动选择", "type": "select", "proxies": names + ["自动选择", "DIRECT"]},
        ],
        "rules": ["MATCH,手动选择"],
    }
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
    return len(proxies)
