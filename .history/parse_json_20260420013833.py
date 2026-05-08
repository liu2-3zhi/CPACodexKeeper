#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用 JSON 解析脚本
支持：直接传文件路径参数
"""

import json
import sys

def extract_details(data):
    results = []
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
    return results

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python parse_json.py <json文件路径>")
        sys.exit(1)

    file_path = sys.argv[1]
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    results = extract_details(data)
    for line in results:
        print(line)
