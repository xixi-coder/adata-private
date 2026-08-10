import unittest

from dashboard.server import normalize_watchlist
from dashboard.xueqiu_analysis import analyze_posts, normalize_influencers


class NormalizeWatchlistTest(unittest.TestCase):
    def test_infers_market_and_removes_duplicates(self):
        result = normalize_watchlist({
            'items': [
                {'code': '600519', 'sector': '白酒'},
                {'code': '300750', 'sector': '电池'},
                {'code': '600519', 'sector': '重复项'},
            ]
        })

        self.assertEqual(result, [
            {'code': '600519', 'market': '1', 'sector': '白酒'},
            {'code': '300750', 'market': '0', 'sector': '电池'},
        ])

    def test_rejects_invalid_codes(self):
        for code in ['', '12345', '1234567', 'ABC123']:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, '6 位数字'):
                normalize_watchlist({'items': [{'code': code}]})

    def test_limits_item_count(self):
        items = [{'code': f'{index:06d}'} for index in range(21)]

        with self.assertRaisesRegex(ValueError, '最多支持 20'):
            normalize_watchlist({'items': items})


if __name__ == '__main__':
    unittest.main()


class XueqiuAnalysisTest(unittest.TestCase):
    def test_normalizes_users_and_rejects_invalid_uid(self):
        self.assertEqual(normalize_influencers({'items': [
            {'uid': '123', 'name': '甲'}, {'uid': '123', 'name': '重复'},
        ]}), [{'uid': '123', 'name': '甲'}])
        with self.assertRaisesRegex(ValueError, '必须是数字'):
            normalize_influencers({'items': [{'uid': 'abc', 'name': '甲'}]})

    def test_analyzes_sentiment_stocks_and_topics(self):
        result = analyze_posts([{
            'id': '1', 'uid': '123', 'publish_time': '2026-08-07 10:00',
            'content': '<b>看好</b> $贵州茅台(SH600519)$ 增长机会 #白酒#',
        }], [{'uid': '123', 'name': '研究员'}])
        self.assertEqual(result['metrics']['bullishPct'], 100)
        self.assertEqual(result['stocks'][0]['code'], 'SH600519')
        self.assertEqual(result['topics'][0]['name'], '白酒')
        self.assertEqual(result['posts'][0]['content'], '看好 $贵州茅台(SH600519)$ 增长机会 #白酒#')
