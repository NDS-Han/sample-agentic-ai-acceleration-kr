#!/usr/bin/env python3
"""18-websearch-client-sim.py — replay what Cowork does, against a live gateway, and judge it.

WHAT: a minimal agentic client for the gateway's server-side web search. It does the three
      things of the real client that matter to the search loop:
        1. identifies itself as Cowork (so the gateway answers in native-block mode),
        2. replays every assistant content block VERBATIM in the next request
           (server_tool_use / web_search_tool_result / text / tool_use),
        3. answers client tool calls (TaskCreate / TaskUpdate / Write) with a tool_result
           and continues — one user prompt becomes several requests, like in Cowork.
      Each prompt is judged against its expectations; exit code 1 if anything fails.
WHY:  Claude Code, Cowork and the models change underneath the gateway. Run this after every
      client or model update, and after every change to web_search_loop.py, BEFORE asking a
      person to test by hand. It is not a copy of Cowork (3 tools instead of ~40, no 40k-token
      system prompt, no thinking) — a real Cowork run stays the final check.
COST: real model calls and real searches (Opus: roughly $0.1–0.4 per scenario).

What the client cannot see: searches the gateway dropped or refused. Read those from the
gateway log events  web_search.mixed_turn_ran / web_search.final_turn /
web_search.search_refused_budget  (kubectl logs deploy/llm-gateway-gateway-proxy).

Usage:
  export GATEWAY_URL=https://gateway.example.com     # or ANTHROPIC_BASE_URL
  export VK_FILE=~/.vk                               # file holding one virtual key (chmod 600)
  python3 18-websearch-client-sim.py                 # built-in scenarios
  python3 18-websearch-client-sim.py --only file     # scenarios whose name contains "file"
  python3 18-websearch-client-sim.py --client claude-code
      # identify as Claude Code instead of Cowork. Which trace mode that class gets is the
      # gateway's setting (WEB_SEARCH_TRACE_NATIVE_CLIENTS): native blocks + 🔎 line, or the
      # 🔎 line alone. The judgement follows what actually came back. Run both classes.
  python3 18-websearch-client-sim.py --scenarios my.json --out transcript.json
  python3 18-websearch-client-sim.py --no-stream     # the gateway's non-streaming loop
  python3 18-websearch-client-sim.py --connect-to 127.0.0.1:8443
      # TCP goes to a local tunnel; TLS name and Host header stay GATEWAY_URL's host
      # (same idea as curl --connect-to). For gateways reachable only from inside.

The key is read from VK_FILE only — never pass it on the command line.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import sys
import time
from urllib.parse import urlparse

SYSTEM = ("You are Cowork, an agentic desktop assistant. For multi-step work, track progress "
          "with TaskCreate and TaskUpdate. Save files with Write when the user asks for a "
          "file. Answer in the user's language.")
TOOLS = [
    {"name": "TaskCreate", "description": "Create a task in the user's visible task list.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}},
                      "required": ["subject"]}},
    {"name": "TaskUpdate", "description": "Update a task's status.",
     "input_schema": {"type": "object", "properties": {"taskId": {"type": "string"},
                      "status": {"type": "string"}}, "required": ["taskId", "status"]}},
    {"name": "Write", "description": "Write a file to the user's workspace.",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"},
                      "content": {"type": "string"}}, "required": ["file_path", "content"]}},
]
TRACE = "[gateway web_search]"
RAN = re.compile(r"— (\d+ results?|결과 \d+건)")   # a 🔎 line of a search that returned results
#: request headers per client class (what gateway-proxy's client_identifier looks at)
CLIENTS = {
    "cowork": {"anthropic-client-platform": "desktop_app",
               "user-agent": "claude-cli/2.1.0 (external, local-agent)"},
    "claude-code": {"user-agent": "claude-cli/2.1.0 (external, cli)"},
}

# expect keys: min_searches, max_searches, min_answer_chars, tool (client tool that must run)
SCENARIOS = [
    {"name": "replay: answer, cite without searching, admit the searches",
     "prompts": [
         {"text": "HBM4 양산 일정을 삼성·SK하이닉스·마이크론별로 정리해줘",
          "expect": {"min_searches": 1, "min_answer_chars": 600}},
         {"text": "방금 답에서 마이크론 일정의 출처가 뭐였지? 새로 검색하지 말고 답해",
          "expect": {"max_searches": 0, "min_answer_chars": 100}},
         {"text": "방금 실제로 검색한 거 맞아? 몇 번 검색했어?",
          "expect": {"max_searches": 0, "min_answer_chars": 20}}]},
    {"name": "no search needed",
     "prompts": [{"text": "파이썬에서 리스트와 튜플의 차이를 세 줄로 설명해줘",
                  "expect": {"max_searches": 0, "min_answer_chars": 50}}]},
    {"name": "budget: many companies in one answer",
     "prompts": [{"text": "TSMC, 삼성 파운드리, 인텔 파운드리, SMIC의 최근 분기 매출과 "
                          "가동률을 표로 비교해줘",
                  "expect": {"min_searches": 2, "min_answer_chars": 800}}]},
    {"name": "file: search, then save with a client tool",
     "prompts": [{"text": "삼성전자 최근 뉴스를 검색해서 요약을 samsung-news.md 파일로 저장해줘",
                  "expect": {"min_searches": 1, "tool": "Write"}}]},
    {"name": "tasks: task list and searches together",
     "prompts": [{"text": "작업 목록을 만들어 진행 상황을 추적하면서, 엔비디아와 AMD의 최근 "
                          "분기 실적을 검색해 비교해줘",
                  "expect": {"min_searches": 1, "min_answer_chars": 500}}]},
]


class Gateway:
    def __init__(self, base_url: str, key: str, model: str, connect_to: str | None,
                 client: str = "cowork", stream: bool = True):
        self.client, self.stream = client, stream
        u = urlparse(base_url)
        if u.scheme != "https" or not u.hostname:
            sys.exit(f"GATEWAY_URL must be https://host[:port] — got {base_url!r}")
        self.host, self.port, self.key, self.model = u.hostname, u.port or 443, key, model
        self.connect_to = None
        if connect_to:
            h, _, p = connect_to.rpartition(":")
            self.connect_to = (h or "127.0.0.1", int(p))

    def _conn(self) -> http.client.HTTPSConnection:
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=400)
        if self.connect_to:   # TCP to the tunnel; TLS (SNI + certificate) still for self.host
            target = self.connect_to
            conn._create_connection = lambda _addr, *a, **k: socket.create_connection(target, *a, **k)
        return conn

    def request(self, messages: list) -> tuple[int, list[dict], str | None, dict, str]:
        """One /v1/messages call (streamed, or one JSON body with --no-stream) →
        (status, content blocks, stop_reason, usage, error)."""
        body = json.dumps({"model": self.model, "max_tokens": 16000, "stream": self.stream,
                           "system": SYSTEM, "tools": TOOLS, "messages": messages})
        conn = self._conn()
        conn.request("POST", "/v1/messages?beta=true", body=body.encode(), headers={
            "Authorization": "Bearer " + self.key, "content-type": "application/json",
            "anthropic-version": "2023-06-01", **CLIENTS[self.client]})
        resp = conn.getresponse()
        if resp.status != 200:
            return resp.status, [], None, {}, resp.read().decode("utf-8", "replace")[:400]
        if not self.stream:                       # one JSON body, blocks already assembled
            d = json.loads(resp.read())
            conn.close()
            content = [b for b in d.get("content") or []
                       if not (b.get("type") == "text" and not b.get("text"))]
            err = json.dumps(d["error"], ensure_ascii=False)[:300] if d.get("error") else ""
            return 200, content, d.get("stop_reason"), d.get("usage") or {}, err
        blocks: dict[int, dict] = {}
        stop, usage, error = None, {}, ""
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            t = ev.get("type")
            if t == "content_block_start":
                b = dict(ev["content_block"])
                if b["type"] in ("tool_use", "server_tool_use"):
                    b["_json"] = ""
                blocks[ev["index"]] = b
            elif t == "content_block_delta":
                b, d = blocks[ev["index"]], ev["delta"]
                if d["type"] == "text_delta":
                    b["text"] = b.get("text", "") + d["text"]
                elif d["type"] == "input_json_delta":
                    b["_json"] += d.get("partial_json", "")
            elif t == "content_block_stop":
                b = blocks[ev["index"]]
                if "_json" in b:
                    b["input"] = json.loads(b.pop("_json") or "{}")
            elif t == "message_start":
                usage.update(ev["message"].get("usage") or {})
            elif t == "message_delta":
                stop = ev["delta"].get("stop_reason", stop)
                usage.update(ev.get("usage") or {})
            elif t == "error":
                error = json.dumps(ev.get("error"), ensure_ascii=False)[:300]
        conn.close()
        content = [blocks[i] for i in sorted(blocks)
                   if not (blocks[i]["type"] == "text" and not blocks[i].get("text"))]
        return 200, content, stop, usage, error


def run_prompt(gw: Gateway, messages: list, prompt: dict, log: list) -> list[str]:
    """One user prompt → as many requests as the client tools need. Returns the failures."""
    text, expect = prompt["text"], prompt.get("expect") or {}
    messages.append({"role": "user", "content": text})
    print(f"\n  USER: {text}")
    answer, searches, refused, tools, fails = "", 0, 0, [], []
    modes: set[str] = set()
    for n in range(1, 11):
        t0 = time.time()
        status, content, stop, usage, error = gw.request(messages)
        if status != 200 or error:
            fails.append(f"request {n}: HTTP {status} {error}")
            break
        messages.append({"role": "assistant", "content": content})
        srv = [b for b in content if b["type"] == "server_tool_use"]
        errs = [b for b in content if b["type"] == "web_search_tool_result"
                and isinstance(b.get("content"), dict)]
        calls = [b for b in content if b["type"] == "tool_use"]
        body = "\n".join(b["text"] for b in content if b["type"] == "text")
        lines = [ln for ln in body.splitlines() if TRACE in ln]
        if srv:                         # native mode: one block pair AND one line per search
            if len(lines) != len(srv):
                fails.append(f"request {n}: {len(srv)} search block(s) but {len(lines)} "
                             "trace line(s)")
            ran_n, refused_n = len(srv) - len(errs), len(errs)
        else:                           # text mode (or no search): the 🔎 line is the only trace
            ran_n = sum(1 for ln in lines if RAN.search(ln))
            refused_n = len(lines) - ran_n
        modes.add("native" if srv else "text" if lines else "")
        searches += ran_n
        refused += refused_n
        tools += [b["name"] for b in calls]
        answer += "\n" + "\n".join(ln for ln in body.splitlines() if TRACE not in ln)
        print(f"    req{n}: {time.time() - t0:3.0f}s stop={stop} searches={ran_n}"
              f" refused={refused_n} client_tools={[b['name'] for b in calls]}"
              f" cache_read={usage.get('cache_read_input_tokens')} out={usage.get('output_tokens')}")
        log.append({"prompt": text, "request": n, "stop": stop, "content": content, "usage": usage})
        if stop != "tool_use" or not calls:
            if stop != "end_turn":
                fails.append(f"request {n}: ended with stop_reason={stop}")
            break
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b["id"],
             "content": (f"File written: {b['input'].get('file_path')}" if b["name"] == "Write"
                         else "ok")} for b in calls]})
    else:
        fails.append("no final answer within 10 requests")
    answer = answer.strip()
    if searches < expect.get("min_searches", 0):
        fails.append(f"searches run {searches} < {expect['min_searches']}")
    if "max_searches" in expect and searches > expect["max_searches"]:
        fails.append(f"searches run {searches} > {expect['max_searches']}")
    if len(answer) < expect.get("min_answer_chars", 0):
        fails.append(f"answer {len(answer)} chars < {expect['min_answer_chars']}")
    if expect.get("tool") and expect["tool"] not in tools:
        fails.append(f"client tool {expect['tool']} was never called")
    print(f"    => {'PASS' if not fails else 'FAIL'}: searches={searches} refused={refused} "
          f"trace={'+'.join(sorted(modes - {''})) or '-'} client_tools={tools} "
          f"answer_chars={len(answer)}")
    for f in fails:
        print(f"       - {f}")
    print("       " + answer[:300].replace("\n", " / "))
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenarios", help="JSON file with the same shape as the built-in list")
    ap.add_argument("--only", help="run scenarios whose name contains this text")
    ap.add_argument("--model", default=os.environ.get("SIM_MODEL", "claude-opus-5"))
    ap.add_argument("--connect-to", help="HOST:PORT of a local tunnel to the gateway")
    ap.add_argument("--client", choices=sorted(CLIENTS), default="cowork",
                    help="client class to identify as (default: cowork)")
    ap.add_argument("--no-stream", action="store_true",
                    help="send non-streaming requests (the gateway's other loop)")
    ap.add_argument("--out", help="write the full transcript (every content block) here")
    a = ap.parse_args()
    base = os.environ.get("GATEWAY_URL") or os.environ.get("ANTHROPIC_BASE_URL")
    key_file = os.environ.get("VK_FILE")
    if not base or not key_file:
        sys.exit("set GATEWAY_URL (or ANTHROPIC_BASE_URL) and VK_FILE — see the header")
    with open(os.path.expanduser(key_file)) as fh:
        key = fh.read().strip()
    gw = Gateway(base, key, a.model, a.connect_to, a.client, stream=not a.no_stream)
    scenarios = SCENARIOS
    if a.scenarios:
        with open(a.scenarios) as fh:
            scenarios = json.load(fh)
    if a.only:
        scenarios = [s for s in scenarios if a.only in s["name"]]
    failed, transcript = [], {}
    print(f"gateway={gw.host} client={a.client} model={a.model} "
          f"stream={gw.stream} scenarios={len(scenarios)} "
          f"start={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    for sc in scenarios:
        print(f"\n=== {sc['name']}")
        messages, log = [], []
        for p in sc["prompts"]:
            if run_prompt(gw, messages, p, log):
                failed.append(f"{sc['name']} :: {p['text'][:40]}")
        transcript[sc["name"]] = log
        if a.out:
            with open(a.out, "w") as fh:
                json.dump(transcript, fh, ensure_ascii=False, indent=1)
    print(f"\nend={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
          f"{'ALL PASS' if not failed else 'FAILED: ' + str(len(failed))}")
    for f in failed:
        print("  - " + f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
