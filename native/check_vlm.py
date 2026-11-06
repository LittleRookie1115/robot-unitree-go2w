#!/usr/bin/env python3
"""Check inherited VLM configuration and /models without a chat request or secret output."""
import argparse
import errno
import json
import math
import os
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def probe(environ, timeout=3.0, opener=None):
    values = {
        name: environ.get("ROBONIX_VLM_" + alias) or environ.get("VLM_" + name, "")
        for name, alias in (("BASE_URL", "UPSTREAM"), ("API_KEY", "API_KEY"), ("MODEL", "MODEL"))
    }
    missing = ["VLM_" + name for name, value in values.items() if not value]
    result = {
        "configuration_loaded": not missing, "missing_variables": missing,
        "target_host": None, "target_port": None, "api_reachable": False,
        "models_http_status": None, "configured_model_listed": None,
        "models_check_passed": False, "reason": "configuration_missing",
    }
    if missing:
        return result
    try:
        target = urlsplit(values["BASE_URL"])
        if target.scheme not in ("http", "https") or not target.hostname:
            raise ValueError("invalid model endpoint")
        result.update(target_host=target.hostname,
                      target_port=target.port or (443 if target.scheme == "https" else 80))
        models_url = urlunsplit((target.scheme, target.netloc,
                                target.path.rstrip("/") + "/models", target.query, ""))
    except ValueError:
        result["reason"] = "invalid_base_url"
        return result
    if opener is None:
        handlers = [NoRedirect()]
        if target.hostname in ("localhost", "127.0.0.1", "::1"):
            handlers.append(ProxyHandler({}))
        opener = build_opener(*handlers)
    request = Request(models_url, headers={
        "Accept": "application/json", "Authorization": "Bearer " + values["API_KEY"],
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            result.update(api_reachable=True, models_http_status=response.status)
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            result["reason"] = "invalid_models_response"
            return result
        document = json.loads(data)
        models = document.get("data") if isinstance(document, dict) else None
        if not isinstance(models, list):
            result["reason"] = "invalid_models_response"
            return result
        found = any(isinstance(model, dict) and model.get("id") == values["MODEL"]
                    for model in models)
        result.update(configured_model_listed=found, models_check_passed=found,
                      reason="models_check_passed" if found else "model_not_listed")
    except HTTPError as error:
        result.update(api_reachable=True, models_http_status=error.code,
                      reason="authentication_failed" if error.code in (401, 403) else
                      "models_endpoint_unavailable")
    except (ValueError, UnicodeError):
        result["reason"] = "invalid_models_response"
    except (URLError, OSError, TimeoutError) as error:
        cause = error.reason if isinstance(error, URLError) else error
        if isinstance(cause, ConnectionRefusedError) or getattr(cause, "errno", None) == errno.ECONNREFUSED:
            reason = "connection_refused"
        elif isinstance(cause, (TimeoutError, socket.timeout)):
            reason = "connection_timeout"
        elif isinstance(cause, socket.gaierror):
            reason = "dns_error"
        elif isinstance(cause, ssl.SSLError):
            reason = "tls_error"
        else:
            reason = "connection_failed"
        result["reason"] = reason
    return result


DETAILS = {
    "configuration_missing": "缺少配置变量；检查 ~/.config/robonix/env。",
    "invalid_base_url": "模型 URL 格式不正确。",
    "models_check_passed": "/models 检查通过，配置的模型名在列表中；未执行聊天或推理请求。",
    "connection_refused": "模型端口拒绝连接：启动对应的模型 API，或恢复模型接口使用的 SSH 转发。",
    "connection_timeout": "连接模型 API 超时。",
    "dns_error": "模型 API 域名解析失败。",
    "tls_error": "模型 API TLS 连接失败。",
    "connection_failed": "无法连接模型 API。",
    "authentication_failed": "已连接到模型 API，但认证/权限检查失败。",
    "models_endpoint_unavailable": "模型 API 已响应，但 /models 不可用；检查路径或该 API 是否支持此接口。",
    "invalid_models_response": "API 已响应，但没有返回标准的模型列表。",
    "model_not_listed": "API 已响应，但配置的模型名未出现在 /models 列表中。",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
        parser.error("--timeout must be in (0, 30]")
    result = probe(os.environ, args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["configuration_loaded"]:
            print("[VLM] URL、密钥、模型名已读入；目标 "
                  f"{result['target_host']}:{result['target_port']}。")
        elif result["missing_variables"]:
            print("[VLM] 缺少变量：" + ", ".join(result["missing_variables"]))
        print("[VLM] " + DETAILS[result["reason"]])
    return 0 if result["models_check_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
