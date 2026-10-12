import os
import imaplib
import email
import re
from email.header import decode_header
from flask import Flask, Response

app = Flask(__name__)

# ---------- 环境变量 ----------
YAHOO_EMAIL = os.environ.get("YAHOO_EMAIL")
YAHOO_APP_PASSWORD = os.environ.get("YAHOO_APP_PASSWORD")

IMAP_SERVER = "imap.mail.yahoo.com"
IMAP_PORT = 993


# ---------- 头部解码 ----------
def decode_mime_words(s):
    if not s:
        return ""
    result = ""
    for content, charset in decode_header(s):
        if isinstance(content, bytes):
            try:
                result += content.decode(charset or "utf-8", errors="replace")
            except (LookupError, UnicodeDecodeError):
                result += content.decode("utf-8", errors="replace")
        else:
            result += content
    return result


# ---------- 正文提取 ----------
def extract_bodies(msg):
    """返回 (plain_str, html_str)，可能其中之一为 None"""
    plain_raw = html_raw = None
    plain_charset = html_charset = None

    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain_raw is None:
                plain_raw = part.get_payload(decode=True)
                plain_charset = part.get_content_charset()
            elif ctype == "text/html" and html_raw is None:
                html_raw = part.get_payload(decode=True)
                html_charset = part.get_content_charset()
    else:
        payload = msg.get_payload(decode=True)
        if msg.get_content_type() == "text/html":
            html_raw = payload
            html_charset = msg.get_content_charset()
        else:
            plain_raw = payload
            plain_charset = msg.get_content_charset()

    def _d(raw, cs):
        if raw is None:
            return None
        try:
            return raw.decode(cs or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            return raw.decode("utf-8", errors="replace")

    return _d(plain_raw, plain_charset), _d(html_raw, html_charset)


def collect_attachments(msg):
    """返回 [(filename, size_bytes, content_type), ...]"""
    result = []
    if not msg.is_multipart():
        return result
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype.startswith("multipart/"):
            continue
        if ctype in ("text/plain", "text/html") and not part.get_filename():
            continue
        disp = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename or "attachment" in disp:
            content = part.get_payload(decode=True) or b""
            result.append((
                decode_mime_words(filename) if filename else "(无名文件)",
                len(content),
                ctype,
            ))
    return result


# ---------- HTML 转义 ----------
def esc(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------- 从 Yahoo 取最新邮件 ----------
def fetch_latest_email():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(YAHOO_EMAIL, YAHOO_APP_PASSWORD)
    mail.select("INBOX")

    status, messages = mail.search(None, "ALL")
    if status != "OK" or not messages[0]:
        mail.logout()
        return None

    latest_id = messages[0].split()[-1]
    status, msg_data = mail.fetch(latest_id, "(RFC822)")
    mail.logout()
    if status != "OK":
        return None

    return email.message_from_bytes(msg_data[0][1])


# ---------- 渲染页面 ----------
def render_page(msg):
    subject = decode_mime_words(msg.get("Subject")) or "(无主题)"
    from_addr = decode_mime_words(msg.get("From")) or "(未知发件人)"
    to_addr = decode_mime_words(msg.get("To")) or "(未知收件人)"
    date = msg.get("Date", "(未知日期)")

    plain, html_body = extract_bodies(msg)
    attachments = collect_attachments(msg)

    # 正文区块：优先用 HTML，否则用纯文本
    if html_body:
        body_block = html_body
    elif plain:
        body_block = f"<pre style='white-space:pre-wrap;font-family:inherit'>{esc(plain)}</pre>"
    else:
        body_block = "<p><em>(邮件无正文内容)</em></p>"

    # 附件列表
    if attachments:
        rows = "".join(
            f"<li>📎 {esc(name)} <span style='color:#888'>({size} 字节, {esc(ct)})</span></li>"
            for name, size, ct in attachments
        )
        attach_block = f"<h3>附件</h3><ul>{rows}</ul>"
    else:
        attach_block = ""

    # 简单隔离 HTML 邮件样式，避免污染外层
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{esc(subject)}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Helvetica Neue", sans-serif;
         margin: 0; background: #f5f5f5; }}
  .meta {{ background: #fff; padding: 20px 24px; border-bottom: 1px solid #e0e0e0; }}
  .meta h1 {{ font-size: 20px; margin: 0 0 12px 0; word-break: break-all; }}
  .meta table {{ border-collapse: collapse; font-size: 13px; color: #555; }}
  .meta td {{ padding: 2px 10px 2px 0; vertical-align: top; }}
  .meta td.k {{ color: #888; white-space: nowrap; }}
  .body {{ background: #fff; margin: 16px; padding: 20px 24px;
           border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.06);
           overflow-x: auto; }}
  .attach {{ background: #fff; margin: 0 16px 24px; padding: 16px 24px;
             border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.06);
             font-size: 14px; }}
  .attach h3 {{ margin: 0 0 8px; font-size: 14px; color: #333; }}
  .attach ul {{ margin: 0; padding-left: 20px; }}
  .error {{ margin: 40px auto; max-width: 600px; background: #fff;
            border-left: 4px solid #d33; padding: 20px 24px;
            font-family: sans-serif; }}
</style>
</head>
<body>
  <div class="meta">
    <h1>{esc(subject)}</h1>
    <table>
      <tr><td class="k">发件人</td><td>{esc(from_addr)}</td></tr>
      <tr><td class="k">收件人</td><td>{esc(to_addr)}</td></tr>
      <tr><td class="k">日期</td><td>{esc(date)}</td></tr>
    </table>
  </div>
  <div class="body">{body_block}</div>
  {f'<div class="attach">{attach_block}</div>' if attach_block else ''}
</body>
</html>"""
    return page


def render_error(message):
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>错误</title></head>
<body style="font-family:sans-serif;background:#f5f5f5">
<div class="error" style="margin:40px auto;max-width:600px;background:#fff;
     border-left:4px solid #d33;padding:20px 24px;">
  <h2 style="margin-top:0">出错了</h2>
  <pre style="white-space:pre-wrap">{esc(message)}</pre>
</div>
</body></html>"""


# ---------- 路由 ----------
@app.route("/")
def index():
    try:
        msg = fetch_latest_email()
        if msg is None:
            return Response(render_error("收件箱为空或获取失败"), mimetype="text/html", status=500)
        return Response(render_page(msg), mimetype="text/html")
    except Exception as e:
        return Response(render_error(str(e)), mimetype="text/html", status=500)


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
