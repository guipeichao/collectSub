import argparse
import copy
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
import yaml


TEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_CONTROLLER = "http://127.0.0.1:19090"
DEFAULT_TIMEOUT_MS = 8000
MAX_WORKERS = 12


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise ValueError(f"不是有效的 Clash 配置：{path}")
    return data


def wait_for_core(controller, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = requests.get(f"{controller}/version", timeout=2)
            if response.ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def test_proxy(controller, name, timeout_ms):
    endpoint = f"{controller}/proxies/{quote(name, safe='')}/delay"
    try:
        response = requests.get(endpoint, params={"url": TEST_URL, "timeout": timeout_ms}, timeout=timeout_ms / 1000 + 3)
        if not response.ok:
            return name, False, f"HTTP {response.status_code}"
        delay = response.json().get("delay")
        if isinstance(delay, int) and delay > 0:
            return name, True, f"{delay}ms"
        return name, False, "无有效延迟"
    except (requests.RequestException, ValueError) as exc:
        return name, False, str(exc)[:160]


def filter_groups(groups, healthy_names):
    filtered = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        item = copy.deepcopy(group)
        if isinstance(item.get("proxies"), list):
            choices = [name for name in item["proxies"] if name in healthy_names or name == "DIRECT"]
            if not choices:
                continue
            item["proxies"] = choices
        filtered.append(item)
    return filtered


def write_tested_config(config, healthy_names, output_path):
    tested = copy.deepcopy(config)
    tested["proxies"] = [proxy for proxy in config["proxies"] if proxy.get("name") in healthy_names]
    tested["proxy-groups"] = filter_groups(config.get("proxy-groups"), healthy_names)
    if not tested["proxies"]:
        raise ValueError("没有通过测试的节点，拒绝覆盖测试版配置")
    temporary_path = f"{output_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(tested, handle, allow_unicode=True, sort_keys=False)
    os.replace(temporary_path, output_path)


def main():
    parser = argparse.ArgumentParser(description="使用 Mihomo API 筛选可用 Clash 节点")
    parser.add_argument("--input", default="sub/merged.yaml")
    parser.add_argument("--output", default="sub/merged-tested.yaml")
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    args = parser.parse_args()

    config = load_config(args.input)
    names = [proxy.get("name") for proxy in config["proxies"] if isinstance(proxy, dict) and proxy.get("name")]
    if not names:
        raise ValueError("汇总配置中没有节点")
    if not wait_for_core(args.controller):
        raise RuntimeError("Mihomo 控制接口未在规定时间内启动")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(test_proxy, args.controller, name, args.timeout_ms) for name in names]
        for future in as_completed(futures):
            results.append(future.result())

    healthy = {name for name, ok, _ in results if ok}
    failed = [(name, reason) for name, ok, reason in results if not ok]
    print(f"节点测试完成：总数 {len(names)}，可用 {len(healthy)}，失败 {len(failed)}")
    for name, reason in failed[:20]:
        print(f"失败：{name}：{reason}")
    if not healthy:
        raise RuntimeError("没有节点通过测试，保留旧的测试版配置")
    write_tested_config(config, healthy, args.output)
    print(f"已写入：{args.output}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"健康检查失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
