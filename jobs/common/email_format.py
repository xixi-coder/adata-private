# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import re
from email.message import EmailMessage
from typing import Optional


_SECTION_EMOJI = {
    "市场": "🌤️",
    "环境": "🌤️",
    "资金": "💧",
    "主题": "🔥",
    "热股": "⭐",
    "降温": "🧊",
    "分歧": "⚖️",
    "波动": "📈",
    "收敛": "🧲",
    "扩张": "🚀",
    "异常": "⚠️",
    "风险": "🛡️",
    "提示": "💡",
    "买入": "🟢",
    "卖出": "🔴",
    "持仓": "📌",
    "候选": "🎯",
}


def _emoji_for(text: str, default: str = "📊") -> str:
    for keyword, emoji in _SECTION_EMOJI.items():
        if keyword in text:
            return emoji
    return default


def _format_inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(
        r"([+-]\d+(?:\.\d+)?%)",
        lambda m: (
            f'<span style="color:#0f8a44;font-weight:700;">{m.group(1)}</span>'
            if m.group(1).startswith("+")
            else f'<span style="color:#c73535;font-weight:700;">{m.group(1)}</span>'
        ),
        escaped,
    )
    escaped = re.sub(
        r"(高|强|快速升温|持续发酵|新晋升温|分批加仓|建议买入)",
        r'<span style="color:#0f8a44;font-weight:700;">\1</span>',
        escaped,
    )
    escaped = re.sub(
        r"(风险|异常|降温|减仓|卖出|失效|退市|处罚|冻结)",
        r'<span style="color:#c73535;font-weight:700;">\1</span>',
        escaped,
    )
    return escaped


def _looks_like_heading(line: str) -> bool:
    return bool(
        re.match(r"^[一二三四五六七八九十]+、", line)
        or line.endswith(":")
        or line in {"使用提示", "风险提示", "票质过滤剔除原因"}
    )


def _render_table_row(line: str) -> str:
    cells = [cell.strip() for cell in line.split("|")]
    body = "".join(
        '<td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;">'
        f"{_format_inline(cell)}</td>"
        for cell in cells
    )
    return f"<tr>{body}</tr>"


def render_email_html(body: str, title: Optional[str] = None, preheader: Optional[str] = None) -> str:
    """Render the project's compact text/Markdown-ish reports as styled email HTML."""
    lines = body.strip().splitlines()
    first_text = next((line.strip() for line in lines if line.strip()), title or "A股监控邮件")
    display_title = title or first_text
    preheader_text = preheader or next((line.strip() for line in lines[1:] if line.strip()), "")

    blocks: list[str] = []
    in_list = False
    in_table = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            blocks.append("</ul>")
            in_list = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            blocks.append("</tbody></table>")
            in_table = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            close_list()
            close_table()
            blocks.append('<div style="height:10px;line-height:10px;">&nbsp;</div>')
            continue
        if line == first_text:
            continue

        table_candidate = "|" in line and not line.startswith("- ")
        if table_candidate:
            close_list()
            if not in_table:
                blocks.append(
                    '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
                    'style="border-collapse:collapse;margin:8px 0 14px;border:1px solid #e5e7eb;'
                    'border-radius:8px;overflow:hidden;background:#ffffff;font-size:13px;">'
                    "<tbody>"
                )
                in_table = True
            blocks.append(_render_table_row(line))
            continue

        close_table()
        bullet = re.match(r"^- (.+)", line)
        numbered = re.match(r"^\d+\.\s+(.+)", line)
        if bullet or numbered:
            if not in_list:
                blocks.append('<ul style="margin:8px 0 14px;padding-left:22px;">')
                in_list = True
            item_text = bullet.group(1) if bullet else numbered.group(1)
            blocks.append(f'<li style="margin:6px 0;">{_format_inline(item_text)}</li>')
            continue

        close_list()
        if _looks_like_heading(line):
            emoji = _emoji_for(line)
            blocks.append(
                '<h2 style="font-size:17px;line-height:1.35;margin:20px 0 8px;color:#111827;">'
                f'<span style="margin-right:6px;">{emoji}</span>{_format_inline(line.rstrip(":"))}</h2>'
            )
        else:
            blocks.append(
                '<p style="margin:7px 0;line-height:1.65;color:#374151;">'
                f"{_format_inline(line)}</p>"
            )

    close_list()
    close_table()

    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f6f8fb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,'Noto Sans CJK SC','Microsoft YaHei',sans-serif;color:#111827;">
    <div style="display:none;max-height:0;overflow:hidden;color:transparent;">{html.escape(preheader_text)}</div>
    <div style="max-width:760px;margin:0 auto;padding:24px 12px;">
      <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
        <div style="padding:22px 24px;background:#111827;color:#ffffff;">
          <div style="font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#a7f3d0;">Market Radar</div>
          <h1 style="font-size:24px;line-height:1.3;margin:6px 0 0;">{_emoji_for(display_title)} {html.escape(display_title)}</h1>
          <div style="font-size:14px;line-height:1.5;margin-top:8px;color:#d1d5db;">{html.escape(preheader_text)}</div>
        </div>
        <div style="padding:22px 24px;font-size:14px;line-height:1.6;">
          {''.join(blocks)}
        </div>
      </div>
      <div style="padding:12px 4px 0;color:#6b7280;font-size:12px;line-height:1.5;">
        本邮件为自动生成的研究提醒，仅供复盘参考，不构成投资建议。
      </div>
    </div>
  </body>
</html>"""


def set_rich_email_content(
    msg: EmailMessage,
    body: str,
    title: Optional[str] = None,
    preheader: Optional[str] = None,
) -> None:
    """Attach plain text plus a styled HTML alternative to an EmailMessage."""
    msg.set_content(body)
    msg.add_alternative(render_email_html(body, title=title, preheader=preheader), subtype="html")
