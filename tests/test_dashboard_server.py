import unittest

from dashboard.server import normalize_watchlist


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
