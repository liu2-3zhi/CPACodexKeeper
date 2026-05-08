#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用 JSON 解析脚本
功能：遍历 usage.apis 下所有模型，提取 details 中的 source 和 timestamp
"""

import json
import sys

def extract_details(data):
    results = []
    try:
        apis = data.get("usage", {}).get("apis", {})
        for api_id, api_data in apis.items():
            models = api_data.get("models", {})
            for model_name, model_data in models.items():
                details = model_data.get("details", [])
                for d in details:
                    source = d.get("source")
                    timestamp = d.get("timestamp")
                    if source and timestamp:
                        results.append(f"{source}\t{timestamp}")
    except Exception as e:
        print(f"解析错误: {e}", file=sys.stderr)
    return results

if __name__ == "__main__":
    # 从 stdin 读取 JSON
    raw = sys.stdin.read()
    data = json.loads(raw)

    results = extract_details(data)
    for line in results:
        print(line)
