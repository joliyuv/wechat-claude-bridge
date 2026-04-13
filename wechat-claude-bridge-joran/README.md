# 微信 + Claude Code 桥接器

> **by [JORAN](https://joran.online)** · 免费开源

> 在微信里直接使用 Claude Code，不用开电脑，不用打开终端

**原理超简单：** 微信 ClawBot 收到你的消息 → 转发给本地 Claude Code → 把回复发回微信

---

## 它能做什么

- 在微信里跟 Claude Code 聊天，多轮对话那种
- 问代码问题让它帮你写
- 让它帮你翻译、写作、头脑风暴
- 发语音也行（微信会自动转文字）

---

## 准备工作

### 1. 你需要有这些东西

- Node.js（用来跑 Claude Code）
- Python 3.8+
- 一个 Claude API Key（去 [ Anthropic 官网](https://console.anthropic.com/) 或第三方网站申请）
- 微信里添加 **ClawBot** 机器人（搜这个名）

### 2. 安装 Claude Code

```bash
npm install -g @anthropic-ai/claude-code
```

### 3. 安装依赖

```bash
pip install httpx qrcode[pil] pillow
```

### 4. 配置你的 API Key

```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here
```

---

## 第一次怎么用

**第一步：** 运行程序

```bash
python wechat_claude_bridge.py
```

终端会出现一个二维码。

**第二步：** 打开微信，扫二维码

在微信里找 ClawBot，点进去扫。

**第三步：** 告诉它你的微信 ID

程序会告诉你需要设置白名单，不然谁都能用你的 Claude：

```bash
export ALLOWED_USERS='你的微信openid'
```

怎么知道自己的 openid？程序日志里会写，第一次运行后给 ClawBot 发条消息，日志就显示出来了。

**第四步：** 重启程序

```bash
python wechat_claude_bridge.py
```

看见「开始监听微信消息」就说明成功了。

---

## 怎么用

直接给 ClawBot 发消息就行：

| 发送 | 效果 |
|------|------|
| 任何文字 | Claude 会回复你 |
| `/reset` | 清空对话历史，重新开始 |
| `/help` | 显示帮助 |
| `/workdir` | 查看当前工作目录 |

---

## 常见问题

**Q: 报 `API Key` 错误**
> 检查 `ANTHROPIC_API_KEY` 环境变量是否设置正确

**Q: 一直显示"正在处理"**
> 检查网络代理设置，或 Claude Code 是否正常运行

**Q: 微信显示机器人不在线**
> 程序可能崩溃了，看终端日志

**Q: Windows 上二维码不显示**
> 安装 `qrcode[pil]` 库后应该能显示，如果还不行会显示链接，手动扫码也行

---

## 安全说明

- **必须配置白名单**，不然任何人都能用你的 Claude
- API Key 只存在你本地，不会发送给任何人
- 凭据文件在 Linux/macOS 上设为 600 权限，Windows 下请保管好你的电脑

---

## 项目结构

```
wechat-claude-bridge/
├── wechat_claude_bridge.py   # 主程序
├── requirements.txt           # Python 依赖
├── README.md                  # 本文件
├── LICENSE                    # MIT 开源协议
└── xhs_card*.html             # 小红书分享图（浏览器打开截图即可）
```

---

## License

MIT
