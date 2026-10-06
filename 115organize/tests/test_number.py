"""番号识别与分类的回归测试。

样本**全部来自他那份真实目录树**（`云下载` 子树的 7 万条路径），不是编的。
跑法（项目根目录）：

    python -m unittest discover -s tests -v
    python tests/test_number.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import number  # noqa: E402


class TestGetId(unittest.TestCase):
    """真值 = 我逐条人工核对过的正确番号。"""

    CASES = [
        # (原始目录名, 期望番号)
        ("DLDSS-532", "DLDSS-532"),
        ("ipzz-916ch", "IPZZ-916"),                       # 小写 + `ch` 后缀
        ("ADN-800-U", "ADN-800"),                         # `-U` 后缀
        ("TSP-381C", "TSP-381"),                          # `C` 后缀
        ("BACJ-079.[4K]@RUNBKK", "BACJ-079"),             # 括号 + @站点
        ("JUR-777_4K60FPS", "JUR-777"),
        ("HUNTC-607-uncensored-HD", "HUNTC-607"),
        ("blk-497ch", "BLK-497"),
        ("madoubt.com 982886.xyz HMN-870", "HMN-870"),    # 前缀一串域名
        ("[无码破解] MMPV-003 ザー面（ヅラ）", "MMPV-003"),
        ("START-424 START系列21无码高清中文字幕影片AI破解作品", "START-424"),
        ("T38-071", "T38-071"),                           # TMA 家的
        ("#_AGEMIX143", "AGEMIX-143"),                    # 缺分隔符
        ("259LUXU-270", "259LUXU-270"),                   # JavSP 特例
        ("fc2-ppv-1567975-HD", "FC2-1567975"),            # FC2
        ("080826_100-PACO", "080826_100"),                # 无码纯数字
    ]

    def test_真实样本(self):
        for raw, want in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(number.get_id(raw), want, f"{raw} 应识别为 {want}")

    def test_300MIUM_数字前缀要带上(self):
        """MGStage 系：`300MIUM-1446` 的系列名就是 `300MIUM`。

        🐛 曾经的 bug：通用规则先跑，切出 `MIUM-1446`（少了数字前缀）。
        """
        self.assertEqual(number.get_id("300MIUM-1446"), "300MIUM-1446")
        self.assertEqual(number.get_id("第一會所新片@SIS001@300MIUM-1446"), "300MIUM-1446")
        self.assertEqual(number.series_of("300MIUM-1446"), "300MIUM")

    def test_污染前缀纠错(self):
        """`manipzz-137` 是站点前缀污染，真实番号 `IPZZ-137`（IPZZ 系列）。

        ⛔ 不能照名字硬认成 `MANIPZZ` 系列 —— 那是错误归属。
        """
        for raw in ("manipzz-137", "MANIPZZ-137", "manipzz-137ch", "Manipzz-137-C"):
            with self.subTest(raw=raw):
                self.assertEqual(number.get_id(raw), "IPZZ-137", f"{raw} 应纠错为 IPZZ-137")
                self.assertEqual(number.series_of(number.get_id(raw)), "IPZZ")
        # 正常 IPZZ 不受影响
        self.assertEqual(number.get_id("IPZZ-137"), "IPZZ-137")
        self.assertEqual(number.get_id("ipzz-916ch"), "IPZZ-916")
        # 不认识的形态原样返回（不瞎改）
        self.assertEqual(number.dedupe_prefix("ABP-303"), "ABP-303")

    def test_无码站日期番号(self):
        """1Pondo 系：`1Pondo-041614_791` —— 硬套通用规则会得到残号 `PONDO-04161`。"""
        self.assertEqual(number.get_id("1Pondo-041614_791-HD"), "PONDO-041614_791")

    def test_不是番号的不能硬认(self):
        """⛔ 这四条是**误判防线** —— 它们被判成番号就会有正片被搬走。"""
        for raw in ("功夫女足.Kung.Fu.Soccer.2026.2160p.YK.HQ.WEB-DL.H265.HDR.DTS-ADWeb",
                    "Vixen.26.09.19.Clemence.Audiard.XXX.1080p.MP4-P2P[XC]",
                    "清道夫.Ray.Donovan.S03E01.中英字幕.HDTVrip.1024X576.mp4",
                    "云下载"):
            with self.subTest(raw=raw):
                self.assertEqual(number.get_id(raw), "")


class TestNormalize(unittest.TestCase):
    """🔴 洗名**绝不能把内容掏空** —— 实测踩过两次，都会导致正片被当广告清理。"""

    def test_洗名不能掏空(self):
        # `fc2-ppv-1567975-nyap2p.com` 曾被域名正则整串吃掉 ⇒ 洗成空串
        self.assertTrue(number.normalize_name("fc2-ppv-1567975-nyap2p.com"))
        self.assertEqual(number.get_id("fc2-ppv-1567975-nyap2p.com"), "FC2-1567975")
        # `@蜂鳥@FENGNIAO151.VIP-IPX-714_2K` 曾被洗成 `蜂鳥`
        self.assertIn("IPX", number.normalize_name("@蜂鳥@FENGNIAO151.VIP-IPX-714_2K"))

    def test_括号里的内容也算噪声(self):
        self.assertEqual(number.normalize_name("【每日更新606dvd.com】50老妇"), "50 老妇".replace(" ", ""))


class TestSeries(unittest.TestCase):
    def test_系列名(self):
        for avid, want in (("DLDSS-532", "DLDSS"), ("FC2-1567975", "FC2"),
                           ("PONDO-041614_791", "PONDO"), ("300MIUM-1446", "300MIUM")):
            with self.subTest(avid=avid):
                self.assertEqual(number.series_of(avid), want)


class TestKind(unittest.TestCase):
    def test_分类(self):
        cases = [
            ("DLDSS-532", number.Kind.JAV),
            ("Vixen.26.09.19.Clemence.XXX.1080p.MP4-P2P[XC]", number.Kind.WESTERN),
            ("清道夫.Ray.Donovan.S03E01.中英字幕.HDTVrip.mp4", number.Kind.TV),
            ("功夫女足.Kung.Fu.Soccer.2026.2160p.WEB-DL.H265.mkv", number.Kind.MOVIE),
            ("云下载", number.Kind.OTHER),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(number.kind_of(raw), want)

    def test_只有技术标签没年份的不算电影(self):
        """`(HD720P)(S-Cute)(no.289)REI` 曾被判成电影、然后被改成 `REI`（信息全丢）。"""
        self.assertNotEqual(number.kind_of("(HD720P)(S-Cute)(no.289)REI"), number.Kind.MOVIE)
        self.assertNotEqual(number.kind_of("6.Underground.2019.1080p.NF.WEBRip.x264-FGT"),
                            number.Kind.WESTERN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
