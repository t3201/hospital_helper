# -*- coding: utf-8 -*-
"""Agent Service 流式推理客户端（纯标准库 http.client/json，零第三方依赖）。

- base URL 解析（SKILL.md §1）: RUNTIME_SERVER_URL → AGENTS_URL
  → http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}
- **无状态调用**: 请求体不带 session_id（不污染 /v1/sessions）；
  `event: init` 里的 session_id 只记录、不依赖。
- SSE 逐行解析: `data: [DONE]` 结束；**只累加 `content` 字段作为答案**；
  `thinking`（思维链）只记长度，绝不并入答案（实测 content="" 时 thinking 有值）。
- 超时: 连接/首字节 ≤ connect_timeout(5s) 快速失败；总时长 ≤ total_timeout(300s)。
- 安全: 请求体只有 model_id+messages；不读/不写/不打任何 key 或凭据
  （/v1/models 响应内含 api_key，本客户端永不访问该端点、永不落盘）。
"""
import http.client
import json
import os
import queue
import re
import socket
import threading
import time
from urllib.parse import urlparse

DEFAULT_MODEL_ID = 'default'  # 注意: 'default' 是 label 不是 model_id, 服务端按 label 解析（实测 200）
CONNECT_FIRST_BYTE_TIMEOUT = 5.0   # 连接+首字节(响应头) 快速失败窗口
TOTAL_TIMEOUT = 300.0              # 整个 SSE 流总预算
READ_CHUNK = 60.0                  # 流内单次读的最大阻塞（数据间隙容忍, 受总预算约束）


class AgentsvcError(RuntimeError):
    """agent-service 调用失败（网络/HTTP 非 200/超时/SSE 中断）。"""


class LLMJsonParseError(AgentsvcError):
    """LLM 输出不是合法 JSON（已按约定重试 1 次仍失败）。"""

    def __init__(self, msg, raw=None):
        super().__init__(msg)
        self.raw = raw
        self.parse_failed = True


def resolve_base_url():
    """RUNTIME_SERVER_URL → AGENTS_URL → http://${HOST:-127.0.0.1}:${PORT:-7988}。
    通配 host(0.0.0.0/::)是服务端 bind 地址, 客户端不可直连（Windows 报 WinError 10049）→ 改写 127.0.0.1。"""
    url = os.environ.get('RUNTIME_SERVER_URL') or os.environ.get('AGENTS_URL')
    if not url:
        host = os.environ.get('RUNTIME_SERVER_HOST', '127.0.0.1')
        port = os.environ.get('RUNTIME_SERVER_PORT', '7988')
        url = 'http://%s:%s' % (host, port)
    u = urlparse(url if '://' in url else 'http://' + url)
    if u.hostname in ('0.0.0.0', '::'):
        url = '%s%s%s' % (u.scheme or 'http', '://127.0.0.1',
                          (':%d' % u.port if u.port else '') + (u.path or ''))
    return url.rstrip('/')


def _host_port_path(base):
    u = urlparse(base if '://' in base else 'http://' + base)
    host = u.hostname or '127.0.0.1'
    port = u.port or (443 if u.scheme == 'https' else 80)
    return host, port, (u.path or '')


def stream_chat(messages, model_id=DEFAULT_MODEL_ID, base=None,
                connect_timeout=CONNECT_FIRST_BYTE_TIMEOUT, total_timeout=TOTAL_TIMEOUT,
                capture_lines=None):
    """POST /v1/infer/stream（SSE）→ 返回:
      content         答案（仅累加 content 字段; thinking 绝不并入）
      thinking_chars  思维链累计长度（仅记录, 不返回内容）
      session_id      init 事件的 session_id（仅记录, 不依赖）
      elapsed_ms / first_byte_ms / n_data
    失败抛 AgentsvcError（连接/首字节 ≤ connect_timeout, 总 ≤ total_timeout）。
    capture_lines: 可选 list, 收集前几条原始 SSE 行（仅供证据/调试, 无凭据）。"""
    base = base or resolve_base_url()
    host, port, prefix = _host_port_path(base)
    path = (prefix.rstrip('/') + '/v1/infer/stream') if prefix else '/v1/infer/stream'
    body = json.dumps({'model_id': model_id, 'messages': messages}, ensure_ascii=False).encode('utf-8')
    t0 = time.monotonic()
    deadline = t0 + total_timeout
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    resp = None
    try:
        try:
            conn.request('POST', path, body=body,
                         headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
            sock_obj = conn.sock  # 先抓住: SSE 响通常 will_close, getresponse 会把 conn.sock 置 None
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise AgentsvcError('连接/首字节失败(≤%.0fs 窗口): %s: %s'
                                % (connect_timeout, type(e).__name__, str(e)[:120]))
        first_byte_ms = (time.monotonic() - t0) * 1000
        if resp.status != 200:
            raw = resp.read(300)
            raise AgentsvcError('HTTP %s: %s' % (resp.status, raw[:200].decode('utf-8', 'replace')))
        # body 读取放到工作线程: SSE 响通常 Connection: close, getresponse 后 conn.sock 被置 None
        # 且 socket 一旦 timeout 即不可复用（'cannot read from timed out object'）,
        # 故工作线程纯阻塞读(timeout=None), 由主线程用 queue+deadline 精确控制"总预算";
        # 否则首 token 间隙 > connect_timeout 时工作线程会误报 TimeoutError。
        try:
            sock_obj.settimeout(None)
        except Exception:
            pass
        q = queue.Queue()

        def _reader():
            try:
                for raw_line in resp:  # 按行迭代至 EOF
                    q.put(raw_line)
                q.put(None)  # EOF 哨兵
            except Exception as e:  # 连接中断等
                q.put(e)
        threading.Thread(target=_reader, daemon=True).start()

        content = ''
        thinking_chars = 0
        session_id = None
        n_data = 0
        cur_event = ''
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentsvcError('总超时(>%.0fs): SSE 未收到 [DONE]' % total_timeout)
            try:
                raw_line = q.get(timeout=min(remaining, READ_CHUNK))
            except queue.Empty:
                continue  # 数据间隙超过当前窗口但总预算未耗尽 → 重新检查 deadline
            if raw_line is None:
                break  # EOF（服务端关闭, 无 [DONE] 也按结束处理）
            if isinstance(raw_line, Exception):
                raise AgentsvcError('SSE 读取中断: %s: %s'
                                    % (type(raw_line).__name__, str(raw_line)[:120]))
            if capture_lines is not None and len(capture_lines) < 10:
                capture_lines.append(raw_line.decode('utf-8', 'replace').rstrip('\n'))
            line = raw_line.decode('utf-8', 'replace').strip()
            if not line:
                cur_event = ''  # 空行 = SSE 事件块分隔
                continue
            if line.startswith(':'):
                continue  # 注释/keep-alive
            if line.startswith('event:'):
                cur_event = line[6:].strip()
                continue
            if line.startswith('data:'):
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                n_data += 1
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue  # 非 JSON data 忽略, 绝不拼进答案
                if not isinstance(obj, dict):
                    continue
                if cur_event == 'init' or obj.get('type') == 'init':
                    session_id = obj.get('session_id') or session_id  # 仅记录
                    cur_event = ''
                    continue
                c = obj.get('content')
                if isinstance(c, str) and c:
                    content += c
                th = obj.get('thinking')
                if isinstance(th, str):
                    thinking_chars += len(th)  # 思维链: 只记长度
                cur_event = ''
                continue
            # id: / 其他行: 忽略
        elapsed_ms = (time.monotonic() - t0) * 1000
        return {'content': content, 'thinking_chars': thinking_chars,
                'session_id': session_id, 'elapsed_ms': round(elapsed_ms, 1),
                'first_byte_ms': round(first_byte_ms, 1), 'n_data': n_data}
    finally:
        if resp is not None:
            try:
                resp.close()  # 关闭底层 socket → 解除工作线程阻塞（will_close 路径 conn.close 无效）
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def _try_parse_json(text):
    """从**答案内容**（不含 thinking）解析第一个完整 JSON 对象; 返回 (obj|None, err)。"""
    t = (text or '').strip()
    if t.startswith('```'):  # 模型若违规包了 markdown 围栏, 先剥掉
        t = re.sub(r'^```[a-zA-Z0-9]*\s*', '', t)
        t = re.sub(r'\s*```$', '', t).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj, None
        return None, '顶层不是 JSON 对象'
    except ValueError:
        pass
    start = t.find('{')
    while start >= 0:  # 花括号配对扫描（字符串/转义感知）
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(t[start:i + 1])
                            if isinstance(obj, dict):
                                return obj, None
                        except ValueError:
                            pass
                        break
        start = t.find('{', start + 1)
    return None, '未找到可解析的 JSON 对象'


def chat_json(system_prompt, user_prompt, model_id=DEFAULT_MODEL_ID, base=None,
              connect_timeout=CONNECT_FIRST_BYTE_TIMEOUT, total_timeout=TOTAL_TIMEOUT,
              capture_lines=None):
    """严格 JSON 一次性调用（四段式 prompt 由调用方构造）。
    解析失败**重试 1 次**（带纠错提示）; 再失败抛 LLMJsonParseError（parse_failed 标记 + raw）。
    返回 (obj, meta)，meta 含 elapsed_ms/attempts/thinking_chars/session_id 等。
    网络类失败（连不上/HTTP 错/超时）不重试, 直接抛 AgentsvcError 供快速回落。"""
    last_raw, last_err = '', ''
    for attempt in (1, 2):
        if attempt == 1:
            prompt = user_prompt
        else:
            prompt = (user_prompt + '\n\n你上一次的输出不是合法 JSON（问题: %s, 原文: %s）。'
                      '请重新输出: 只输出一个 JSON 对象本身, 不要任何解释、前缀或代码块。'
                      % (last_err, last_raw[:400]))
        r = stream_chat([{'role': 'system', 'content': system_prompt},
                         {'role': 'user', 'content': prompt}],
                        model_id=model_id, base=base,
                        connect_timeout=connect_timeout, total_timeout=total_timeout,
                        capture_lines=capture_lines)
        last_raw = r['content']
        obj, err = _try_parse_json(last_raw)
        if obj is not None:
            r['attempts'] = attempt
            r['parse_error'] = None
            return obj, r
        last_err = err or '无'
    raise LLMJsonParseError('LLM 输出非合法 JSON（已重试 1 次仍失败: %s）' % last_err, raw=last_raw)
