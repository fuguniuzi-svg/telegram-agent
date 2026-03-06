import os
import logging
import json
import subprocess
import requests
import certifi
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from bs4 import BeautifulSoup
from telegram import Update, constants, error
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from playwright.async_api import async_playwright

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============ 配置区域（部署时修改这里） ============
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8719859255:AAHSY5VpF2aIxbyUMCnQRI636_FW3DazEfc")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", None)  # 留空则使用默认
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
GMAIL_USER = os.getenv("GMAIL_USER", "fuguniuzi@gmail.com")
GMAIL_PASS = os.getenv("GMAIL_PASS", "gkox bhal gmth keov")
# ====================================================

if not OPENAI_API_KEY:
    raise ValueError("请设置 OPENAI_API_KEY 环境变量")

# 初始化 OpenAI 客户端
client_kwargs = {"api_key": OPENAI_API_KEY, "timeout": 120.0}
if OPENAI_BASE_URL:
    client_kwargs["base_url"] = OPENAI_BASE_URL
client = OpenAI(**client_kwargs)

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

# JS 渲染检测关键词
JS_KEYWORDS = ["enable javascript", "javascript is required", "please enable javascript",
               "noscript", "browser doesn't support", "需要启用 JavaScript"]

# ============ 工具定义 ============
tools = [
    {
        "type": "function",
        "function": {
            "name": "browse_webpage",
            "description": "访问并抓取网页内容。支持普通网页和需要 JavaScript 渲染的动态网页（如 X.com、SPA 应用）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标网页 URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell_command",
            "description": "在服务器上执行 shell 命令并返回输出结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_ops",
            "description": "文件操作：读取(read)、写入(write)、追加(append)、删除(delete)文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["read", "write", "append", "delete"], "description": "操作类型"},
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "写入或追加的内容（read/delete 时可省略）"}
                },
                "required": ["op", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "通过 Telegram 发送本地文件给用户。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要发送的文件路径"}
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "通过 Gmail 发送邮件，支持附件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件人邮箱地址"},
                    "subject": {"type": "string", "description": "邮件主题"},
                    "body": {"type": "string", "description": "邮件正文"},
                    "attachment": {"type": "string", "description": "可选的附件文件路径"}
                },
                "required": ["to", "subject", "body"]
            }
        }
    }
]

SYSTEM_PROMPT = """你是一个全能 AI 助手，运行在 Telegram 上。你既能闲聊也能执行复杂任务。

【对话规则】
- 普通聊天、问答、闲聊时，直接回复，不要调用任何工具
- 只有在用户明确要求执行操作时（如抓取网页、执行命令、操作文件、发邮件等），才调用工具

【任务执行 - ReAct 模式】
当需要执行任务时，遵循以下流程：
1. 思考(Think)：分析用户需求，分解为具体步骤
2. 行动(Act)：调用合适的工具执行
3. 观察(Observe)：检查工具返回结果
4. 反思(Reflect)：结果是否正确？是否需要调整？
   - 如果工具失败，分析原因，尝试换方法或重试
   - 如果结果不完整，补充执行
5. 重复直到任务完成

【自我纠错规则】
- 网页抓取失败 → 自动尝试 Playwright 无头浏览器
- 命令执行失败 → 检查命令语法，修正后重试
- 文件操作失败 → 检查路径是否正确
- 最多重试 2 次，仍然失败则向用户说明原因和建议

【回复风格】
- 用中文回复
- 简洁明了，不要啰嗦
- 闲聊时自然友好
"""


# ============ 辅助函数 ============

async def safe_edit(bot, chat_id, message_id, text):
    """安全地编辑消息，忽略内容未变化的错误"""
    try:
        # Telegram 消息最大 4096 字符
        if len(text) > 4000:
            text = text[:4000] + "\n...(内容过长已截断)"
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except error.BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"编辑消息失败: {e}")
    except Exception as e:
        logger.error(f"编辑消息异常: {e}")


async def scrape_with_playwright(url: str) -> str:
    """使用 Playwright 无头浏览器抓取需要 JS 渲染的网页"""
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            ctx = await browser.new_context(
                user_agent=DEFAULT_HEADERS['User-Agent'],
                viewport={'width': 1920, 'height': 1080}
            )
            page = await ctx.new_page()

            # 设置较长超时，等待页面加载
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception:
                # 即使超时也尝试获取已加载的内容
                pass

            # 额外等待 JS 渲染
            await page.wait_for_timeout(8000)

            # 尝试滚动页面触发懒加载
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(2000)

            content = await page.content()
            await browser.close()

            soup = BeautifulSoup(content, 'html.parser')
            for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
                tag.extract()

            text = soup.get_text(separator='\n')
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            result = '\n'.join(lines)

            if not result or len(result) < 50:
                return "页面内容为空或过少，可能被反爬机制拦截。建议尝试其他网站或提供更具体的 URL。"

            return result[:15000]

    except Exception as e:
        if browser:
            try:
                await browser.close()
            except:
                pass
        return f"Playwright 抓取失败: {str(e)}"


async def browse_webpage(url: str) -> str:
    """智能网页抓取：先尝试 requests，失败或检测到需要 JS 时自动切换 Playwright"""

    # 已知需要 JS 的网站直接用 Playwright
    js_sites = ["x.com", "twitter.com", "instagram.com", "facebook.com", "tiktok.com"]
    if any(site in url for site in js_sites):
        logger.info(f"检测到 JS 网站，直接使用 Playwright: {url}")
        return await scrape_with_playwright(url)

    # 先尝试 requests（更快）
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=20, verify=certifi.where(), allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or 'utf-8'

        # 检查是否需要 JS 渲染
        content_lower = resp.text.lower()
        needs_js = any(kw.lower() in content_lower for kw in JS_KEYWORDS)
        too_short = len(resp.text.strip()) < 500

        if needs_js or too_short:
            logger.info(f"检测到需要 JS 或内容过少，切换 Playwright: {url}")
            return await scrape_with_playwright(url)

        # 解析 HTML
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.extract()

        text = soup.get_text(separator='\n')
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        result = '\n'.join(lines)

        if not result:
            return await scrape_with_playwright(url)

        return result[:15000]

    except requests.exceptions.Timeout:
        logger.info(f"requests 超时，切换 Playwright: {url}")
        return await scrape_with_playwright(url)
    except requests.exceptions.HTTPError as e:
        if e.response and e.response.status_code == 403:
            logger.info(f"403 被拒绝，切换 Playwright: {url}")
            return await scrape_with_playwright(url)
        return f"HTTP 错误: {e}"
    except Exception as e:
        logger.info(f"requests 失败，切换 Playwright: {url}")
        return await scrape_with_playwright(url)


def do_send_email(to, subject, body, attachment=None):
    """发送 Gmail 邮件"""
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        if attachment and os.path.exists(attachment):
            with open(attachment, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(attachment)}")
            msg.attach(part)

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        return f"邮件已成