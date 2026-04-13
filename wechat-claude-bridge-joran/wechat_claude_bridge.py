#!/usr/bin/env python3
"""
微信 ClawBot → Claude Code Bridge
基于腾讯官方 iLink Bot API，通过 `claude -p` 驱动本地 Claude Code。

依赖安装：
    pip install httpx qrcode[pil] pillow

使用方法：
    $env:ANTHROPIC_API_KEY=sk-ant-xxx
    python wechat_claude_bridge.py

首次运行：终端显示二维码 → 微信扫码 → 自动开始桥接
"""

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# 解决 Windows 编码问题
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

try:
    import httpx
except ImportError:
    print("请先安装依赖：pip install httpx qrcode[pil] pillow")
    sys.exit(1)

try:
    import qrcode
except ImportError:
    qrcode = None

# ── 配置 ────────────────────────────────────────────────────────────────────

BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
LONG_POLL_TIMEOUT = 35
MAX_REPLY_LENGTH = 4000
CREDENTIALS_DIR = Path.home() / ".config" / "wechat-claude-bridge"
CREDENTIALS_FILE = CREDENTIALS_DIR / "account.json"
SYNC_BUF_FILE = CREDENTIALS_DIR / "sync_buf.txt"

WORKDIR = os.environ.get("CLAUDE_WORKDIR", str(Path.home()))

# 白名单：必须配置！留空则程序拒绝启动
ALLOWED_USERS: list[str] = []
# 例如：ALLOWED_USERS = ["xxxxxxxx@im.wechat"]
# 或通过环境变量（多个用逗号分隔）：
#   $env:ALLOWED_USERS="openid1@im.wechat,openid2@im.wechat"

# 速率限制：同一用户两条消息的最小间隔（秒）
RATE_LIMIT_SECONDS = 5

# 最大追踪用户数，防止长期运行内存耗尽
MAX_TRACKED_USERS = 1000

# ── 日志 ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def random_wechat_uin() -> str:
    """生成随机 X-WECHAT-UIN header（模拟客户端标识）"""
    import base64
    rand = secrets.randbits(32)
    return base64.b64encode(str(rand).encode()).decode()


def build_headers(token: Optional[str] = None, body: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_wechat_uin(),
    }
    if body:
        headers["Content-Length"] = str(len(body.encode("utf-8")))
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def split_message(text: str, max_len: int = MAX_REPLY_LENGTH) -> list[str]:
    """将长消息拆分为多段"""
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts


# ── Claude 命令发现 ─────────────────────────────────────────────────────────

def find_claude_command() -> str:
    """查找 claude 可执行命令，优先从 PATH，找不到则尝试 Common locations"""
    # 优先从 PATH
    if shutil.which("claude"):
        return "claude"

    # Windows 常见位置
    windows_paths = [
        os.path.expandvars(r"%APPDATA%\npm\claude.ps1"),
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\npm\claude.ps1"),
        r"C:\Program Files\nodejs\claude.ps1",
    ]
    for p in windows_paths:
        if os.path.exists(p):
            return p

    # 最后返回 "claude"，让系统 PATH 去决定
    return "claude"


# ── 凭据存储 ─────────────────────────────────────────────────────────────────

def load_credentials() -> Optional[dict]:
    try:
        if CREDENTIALS_FILE.exists():
            return json.loads(CREDENTIALS_FILE.read_text())
    except Exception:
        pass
    return None


def save_credentials(data: dict) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    try:
        CREDENTIALS_FILE.chmod(0o600)
    except Exception:
        pass  # Windows 上 chmod 无效，忽略
    log.info(f"凭据已保存到 {CREDENTIALS_FILE}")


def load_sync_buf() -> str:
    try:
        if SYNC_BUF_FILE.exists():
            return SYNC_BUF_FILE.read_text().strip()
    except Exception:
        pass
    return ""


def save_sync_buf(buf: str) -> None:
    try:
        SYNC_BUF_FILE.write_text(buf)
    except Exception:
        pass


# ── iLink API ────────────────────────────────────────────────────────────────

class ILinkClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        # 禁用环境代理 + 验证 TLS
        self._client = httpx.AsyncClient(timeout=40.0, verify=True, trust_env=False)

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    async def get_updates(self, get_updates_buf: str) -> dict:
        body = json.dumps({
            "get_updates_buf": get_updates_buf,
            "base_info": {"channel_version": "0.1.0"},
        })
        resp = await self._client.post(
            self._url("ilink/bot/getupdates"),
            content=body,
            headers=build_headers(self.token, body),
            timeout=LONG_POLL_TIMEOUT + 5,
        )
        resp.raise_for_status()
        return resp.json()

    async def send_message(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> None:
        client_id = f"claude-bridge:{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        body = json.dumps({
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
                "context_token": context_token,
            },
            "base_info": {"channel_version": "0.1.0"},
        })
        resp = await self._client.post(
            self._url("ilink/bot/sendmessage"),
            content=body,
            headers=build_headers(self.token, body),
            timeout=15.0,
        )
        resp.raise_for_status()

    async def close(self):
        await self._client.aclose()


# ── 二维码登录 ───────────────────────────────────────────────────────────────

async def qr_login() -> Optional[dict]:
    """扫码登录，返回 account dict 或 None"""
    async with httpx.AsyncClient(timeout=10.0, verify=True, trust_env=False) as client:
        log.info("正在获取微信登录二维码...")
        resp = await client.get(
            f"{BASE_URL}/ilink/bot/get_bot_qrcode",
            params={"bot_type": BOT_TYPE},
        )
        resp.raise_for_status()
        qr_data = resp.json()
        qrcode_val = qr_data["qrcode"]
        qr_content = qr_data.get("qrcode_img_content", qrcode_val)

        _print_qr(qr_content)
        log.info("请用微信扫描上方二维码，然后在微信中点击确认...")
        log.info(f"二维码链接: {qr_content}")

        deadline = time.time() + 480
        scanned = False
        while time.time() < deadline:
            try:
                sr = await client.get(
                    f"{BASE_URL}/ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode_val},
                    headers={"iLink-App-ClientVersion": "1"},
                    timeout=36.0,
                )
                sr.raise_for_status()
                status = sr.json()
            except httpx.TimeoutException:
                continue
            except Exception as e:
                log.warning(f"轮询状态异常: {e}")
                await asyncio.sleep(2)
                continue

            s = status.get("status")
            if s == "wait":
                pass
            elif s == "scaned" and not scanned:
                log.info("已扫码，请在微信中点击确认...")
                scanned = True
            elif s == "expired":
                log.error("二维码已过期，请重新运行。")
                return None
            elif s == "confirmed":
                bot_token = status.get("bot_token")
                bot_id = status.get("ilink_bot_id")
                if not bot_token or not bot_id:
                    log.error("登录确认但返回数据不完整")
                    return None
                account = {
                    "token": bot_token,
                    "base_url": status.get("baseurl", BASE_URL),
                    "account_id": bot_id,
                    "user_id": status.get("ilink_user_id"),
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                save_credentials(account)
                log.info(f"登录成功！Bot ID: {bot_id}")
                return account

            await asyncio.sleep(1)

    log.error("登录超时（8 分钟）")
    return None


def _print_qr(content: str) -> None:
    """在终端打印二维码"""
    if qrcode is not None:
        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    else:
        print(f"\n二维码内容（请手动访问或使用在线二维码生成器扫描）：\n{content}\n")


# ── Claude Code 调用 ─────────────────────────────────────────────────────────

_conversation_history: dict[str, list[dict]] = {}


async def ask_claude(user_id: str, user_text: str) -> str:
    """调用本地 claude -p 命令，通过 stdin 传入 prompt"""
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY") or
        os.environ.get("ANTHROPIC_AUTH_TOKEN") or
        os.environ.get("CLAUDE_API_KEY") or
        ""
    )
    if not api_key:
        return "未设置 API Key 环境变量，请确保 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / CLAUDE_API_KEY 之一已设置。"

    history = _conversation_history.setdefault(user_id, [])
    history.append({"role": "user", "content": user_text})

    prompt_parts = []
    for turn in history[-20:]:
        role_label = "用户" if turn["role"] == "user" else "助手"
        prompt_parts.append(f"[{role_label}]: {turn['content']}")
    full_prompt = "\n".join(prompt_parts)

    claude_cmd = find_claude_command()
    env = os.environ.copy()
    # 把实际用的那个 key 写入标准环境变量名，Claude Code 会自动读取
    env["ANTHROPIC_API_KEY"] = api_key
    env["ANTHROPIC_AUTH_TOKEN"] = api_key

    try:
        # 优先：直接调用 claude（Linux/Mac/Windows PATH）
        proc = await asyncio.create_subprocess_exec(
            claude_cmd, "-p", "-",
            "--output-format", "text",
            cwd=WORKDIR,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=full_prompt.encode("utf-8")),
            timeout=120,
        )
        reply = stdout.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0 and not reply:
            err = stderr.decode("utf-8", errors="replace").strip()
            log.warning(f"claude 进程返回非零: {proc.returncode}, stderr: {err[:200]}")
            if "authentication" in err.lower() or "api key" in err.lower():
                return "API Key 认证失败，请检查 ANTHROPIC_API_KEY 是否正确。"
            if "not found" in err.lower() or "ENOENT" in err:
                # claude 不在 PATH，尝试 PowerShell 方式
                pass
            else:
                return "Claude Code 执行错误，请检查终端日志。"

        if not reply:
            reply = "（Claude 没有返回内容）"

        history.append({"role": "assistant", "content": reply})
        if len(history) > 40:
            _conversation_history[user_id] = history[-40:]

        return reply

    except asyncio.TimeoutError:
        log.error("claude 执行超时 (120s)")
        return "Claude Code 处理超时（120s），请稍后重试或简化问题。"
    except FileNotFoundError:
        # claude 不在 PATH，尝试 PowerShell
        try:
            ps1_path = os.path.expandvars(r"%APPDATA%\npm\claude.ps1")
            if not os.path.exists(ps1_path):
                return (
                    "未找到 claude 命令。\n"
                    "请先安装 Claude Code：\n"
                    "npm install -g @anthropic-ai/claude-code"
                )

            # 用 PowerShell -Command 调用，参数通过 -p 传入（不用 stdin，避免编码问题）
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile", "-Command",
                f"& '{ps1_path}' -p ([Console]::In.ReadToEnd()) --output-format text",
                cwd=WORKDIR,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=120,
            )
            reply = stdout.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0 and not reply:
                err = stderr.decode("utf-8", errors="replace").strip()
                log.warning(f"PowerShell claude 返回非零: {proc.returncode}, stderr: {err[:200]}")
                return "Claude Code 执行错误，请检查终端日志。"

            if not reply:
                reply = "（Claude 没有返回内容）"

            history.append({"role": "assistant", "content": reply})
            if len(history) > 40:
                _conversation_history[user_id] = history[-40:]

            return reply

        except FileNotFoundError:
            return (
                "未找到 claude 命令。\n"
                "请先安装 Claude Code：\n"
                "npm install -g @anthropic-ai/claude-code"
            )
        except asyncio.TimeoutError:
            log.error("claude 执行超时 (120s)")
            return "Claude Code 处理超时（120s），请稍后重试。"
    except Exception as e:
        log.error(f"调用 claude 异常: {e}")
        return f"内部错误: {e}"


# 特殊指令处理
COMMANDS = {
    "/reset": "清空对话历史",
    "/help":  "显示帮助",
    "/workdir": "查看当前工作目录",
}


async def handle_command(user_id: str, text: str) -> Optional[str]:
    """处理 /reset /help 等特殊指令"""
    cmd = text.strip().lower().split()[0] if text.strip() else ""
    if cmd == "/reset":
        _conversation_history.pop(user_id, None)
        return "对话历史已清空，开始新对话。"
    elif cmd == "/help":
        lines = ["Claude Code 微信桥接", "可用指令："]
        for c, desc in COMMANDS.items():
            lines.append(f"  {c} — {desc}")
        lines.append("\n直接发送文字即可与 Claude Code 对话。")
        return "\n".join(lines)
    elif cmd == "/workdir":
        return f"当前工作目录：{WORKDIR}"
    return None


# ── 消息解析 ─────────────────────────────────────────────────────────────────

def extract_text(msg: dict) -> str:
    """从 iLink 消息结构中提取文本内容"""
    item_list = msg.get("item_list") or []
    for item in item_list:
        item_type = item.get("type")
        if item_type == 1:
            text = (item.get("text_item") or {}).get("text", "")
            ref = item.get("ref_msg")
            if ref:
                ref_title = ref.get("title", "")
                if ref_title:
                    text = f"[引用: {ref_title}]\n{text}"
            return text
        elif item_type == 3:  # 语音
            return (item.get("voice_item") or {}).get("text", "")
    return ""


# ── 主循环 ───────────────────────────────────────────────────────────────────

async def run_bridge(account: dict) -> None:
    client = ILinkClient(account["base_url"], account["token"])
    get_updates_buf = load_sync_buf()
    context_token_cache: dict[str, str] = {}
    consecutive_failures = 0

    user_locks: dict[str, asyncio.Lock] = {}
    user_last_message_time: dict[str, float] = {}
    confirmed_users: set[str] = set(ALLOWED_USERS)

    log.info(f"开始监听微信消息（工作目录: {WORKDIR}）")
    log.info("在微信中向 ClawBot 发送消息即可开始对话。")
    log.info(f"白名单已配置，仅响应: {ALLOWED_USERS}")

    async def process_message(msg: dict) -> None:
        """处理单条消息（在锁内运行）"""
        sender_id = msg.get("from_user_id", "unknown")
        context_token = msg.get("context_token", "")
        if context_token:
            context_token_cache[sender_id] = context_token

        text = extract_text(msg)
        if not text:
            return

        # 速率限制检查
        now = time.time()
        last_time = user_last_message_time.get(sender_id, 0)
        if now - last_time < RATE_LIMIT_SECONDS:
            log.info(f"  → 用户 {sender_id[:20]}... 消息过于频繁，忽略")
            return
        user_last_message_time[sender_id] = now

        # 日志脱敏
        safe_text = text[:80] + ("..." if len(text) > 80 else "")
        log.info(f"收到消息: from={sender_id[:20]}... text={safe_text}")

        # 白名单检查
        if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
            log.info(f"  → 用户不在白名单，忽略")
            return

        ctx = context_token_cache.get(sender_id)
        if not ctx:
            log.warning(f"  → 无 context_token，无法回复 {sender_id}")
            return

        reply = await handle_command(sender_id, text)

        if reply is None:
            await client.send_message(sender_id, "正在处理，请稍候...", ctx)
            reply = await ask_claude(sender_id, text)

        parts = split_message(reply)
        for part in parts:
            try:
                await client.send_message(sender_id, part, ctx)
                if len(parts) > 1:
                    await asyncio.sleep(0.5)
            except Exception as e:
                log.error(f"发送消息失败: {e}")

    while True:
        try:
            resp = await client.get_updates(get_updates_buf)

            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            if ret != 0 or errcode != 0:
                consecutive_failures += 1
                log.error(f"getUpdates 失败: ret={ret} errcode={errcode} errmsg={resp.get('errmsg')}")
                if consecutive_failures >= 3:
                    log.warning("连续失败 3 次，等待 30 秒后重试...")
                    consecutive_failures = 0
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(3)
                continue

            consecutive_failures = 0

            new_buf = resp.get("get_updates_buf", "")
            if new_buf:
                get_updates_buf = new_buf
                save_sync_buf(new_buf)

            msgs = resp.get("msgs") or []
            for msg in msgs:
                if msg.get("message_type") != 1:
                    continue

                sender_id = msg.get("from_user_id", "unknown")

                # 追踪活跃用户，超量时清理最老的非白名单用户
                if sender_id not in confirmed_users:
                    if len(confirmed_users) >= MAX_TRACKED_USERS:
                        oldest = min(user_last_message_time, key=user_last_message_time.get)
                        for d in [user_locks, user_last_message_time, _conversation_history]:
                            d.pop(oldest, None)
                        log.info(f"清理过期用户 {oldest[:20]}...，保持用户数上限")
                    confirmed_users.add(sender_id)

                if sender_id not in user_locks:
                    user_locks[sender_id] = asyncio.Lock()

                async def _task(m=msg, sid=sender_id):
                    async with user_locks[sid]:
                        await process_message(m)

                asyncio.create_task(_task())

        except httpx.TimeoutException:
            pass
        except httpx.HTTPStatusError as e:
            consecutive_failures += 1
            log.error(f"HTTP 错误: {e}")
            await asyncio.sleep(5)
        except Exception as e:
            consecutive_failures += 1
            log.error(f"轮询异常: {e}")
            await asyncio.sleep(3)

    await client.close()


# ── 入口 ────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 50)
    print("  微信 ClawBot → Claude Code Bridge")
    print("  基于腾讯官方 iLink Bot API")
    print("=" * 50)

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("CLAUDE_API_KEY")):
        print("\n请先设置 API Key（任选一种）：")
        print("   $env:ANTHROPIC_API_KEY=sk-ant-xxx")
        print("   或 $env:ANTHROPIC_AUTH_TOKEN=sk-api-xxx")
        print("   或 $env:CLAUDE_API_KEY=sk-ant-xxx\n")
        sys.exit(1)

    # 加载白名单配置
    env_allowed = os.environ.get("ALLOWED_USERS", "")
    if env_allowed:
        ALLOWED_USERS.extend([u.strip() for u in env_allowed.split(",") if u.strip()])

    if not ALLOWED_USERS:
        print("\n[安全错误] 必须配置 ALLOWED_USERS 才能启动！")
        print("请设置环境变量或修改代码中的 ALLOWED_USERS 列表：")
        print("   $env:ALLOWED_USERS='你的微信openid@im.wechat'\n")
        print("如何获取 openid：运行一次程序，向 ClawBot 发消息，日志中会显示你的 openid。")
        sys.exit(1)

    # 检查 claude 命令
    claude_cmd = find_claude_command()
    log.info(f"Claude 命令路径: {claude_cmd}")

    # 加载或获取凭据
    account = load_credentials()
    if account:
        log.info(f"使用已保存凭据，Bot ID: {account.get('account_id')}")
    else:
        log.info("未找到已保存凭据，开始扫码登录...")
        account = await qr_login()
        if not account:
            log.error("登录失败，退出。")
            sys.exit(1)

    await run_bridge(account)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")
