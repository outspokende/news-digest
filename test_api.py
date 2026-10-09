#!/usr/bin/env python3
import requests, os, json

api_key = os.environ.get("MINIMAX_API_KEY")
resp = requests.post(
    "https://api.minimax.io/v1/text/chatcompletion_v2",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={"model": "MiniMax-M2.7", "messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "hi"}], "temperature": 0.2, "max_tokens": 50},
    timeout=30
)
print("status:", resp.status_code)
print("body:", resp.text[:500])