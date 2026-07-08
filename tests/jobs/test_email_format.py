# -*- coding: utf-8 -*-
import unittest
from email.message import EmailMessage

from jobs.common.email_format import render_email_html, set_rich_email_content


class EmailFormatTest(unittest.TestCase):
    def test_render_email_html_formats_sections_tables_and_colors(self):
        body = "\n".join(
            [
                "A股波动结构扫描",
                "2026-07-08 | 候选 2 只 | 收敛 1 / 扩张 1 / 异常 0",
                "",
                "一、资金聚焦",
                "1. 半导体 | 2只 | 均分88",
                "- 北向净流入: +12.5%",
                "- 风险提醒: -3.2%",
            ]
        )

        html = render_email_html(body)

        self.assertIn("Market Radar", html)
        self.assertIn("💧", html)
        self.assertIn("<table", html)
        self.assertIn("color:#0f8a44", html)
        self.assertIn("color:#c73535", html)

    def test_set_rich_email_content_keeps_plain_text_fallback(self):
        msg = EmailMessage()

        set_rich_email_content(msg, "雪球监听\n1. 新增自选股", title="雪球监听")

        self.assertTrue(msg.is_multipart())
        plain, html = msg.get_payload()
        self.assertEqual(plain.get_content_type(), "text/plain")
        self.assertEqual(html.get_content_type(), "text/html")
        self.assertIn("新增自选股", plain.get_content())
        self.assertIn("新增自选股", html.get_content())


if __name__ == "__main__":
    unittest.main()
