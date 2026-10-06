"""命名规则的回归测试（规则来自红领巾 2026-10-05 的截图 + 115 接口真值）

跑法（项目根目录）：
    python tests/test_namer.py

样本全部是**真实数据**，不是编的：
  A  护肝人.6v电影 地址发布页 www.6v123.net 收藏不迷路        （任务名，接口确认已改成 护肝人（2026））
  B  【高清影视之家发布 www.BBEGGE.com】大唐妖探[50帧率版本][国语配音+中文字幕].Demon.Agent.2026.…
                                                              （回收站 parent_name 真值 → 目标 大唐妖探（2026））
  C  【高清影视之家发布 www.HDBTHD.com】年会不能停！[IMAX满屏版][国语音轨+中文字幕].Johnny.Keep.Walking.2023.…
                                                              （任务名 → 目标 年会不能停！（系列））
  D  驻院医生.S01E01.Pilot.mkv / 我的媳妇.S01E30.mkv            （截图里的分集文件）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from namer import (  # noqa: E402
    clean,
    clean_file,
    dir_name,
    ensure_ext,
    episode_name,
    extract_year,
    is_episode_file,
    looks_like_ad,
    parse_media,
    sanitize_name,
    suggest,
    title_of,
)

fail = []


def check(name, got, want):
    ok = got == want
    print(("  [OK] " if ok else "  [!!] ") + name)
    if not ok:
        print("       got :", repr(got))
        print("       want:", repr(want))
        fail.append(name)


A = "护肝人.6v电影 地址发布页 www.6v123.net 收藏不迷路"
B = ("【高清影视之家发布 www.BBEGGE.com】大唐妖探[50帧率版本][国语配音+中文字幕]"
     ".Demon.Agent.2026.2160p.WEB-DL.AAC.H265.HDR-ParkHD")
C = ("【高清影视之家发布 www.HDBTHD.com】年会不能停！[IMAX满屏版][国语音轨+中文字幕]"
     ".Johnny.Keep.Walking.2023.IMAX.2160p.WEB-DL.H265.HDR.DTS-GPTHD")
D2 = "寒战1994.Cold.War.2026.2160p.WEB-DL.H265.HDR-DreamHD"

print("=== 保守清洗（保留原语义，只去广告）===")
# ⚠️ 2026-10-05 第二轮改口径：`6v电影` 这类**站点名**以前被当作有效内容留着，
#    现在进了 SITE_WORDS ⇒ 一并清掉（用户反馈「清理功能需要再优化」）。
check("A（站名 6v电影 也清掉）", clean(A), "护肝人")
check("C：保留 [IMAX满屏版] 这类有效信息", "[IMAX满屏版]" in clean(C), True)
check("空串原样", clean(""), "")

print("\n=== 片名抽取 title_of ===")
check("A → 护肝人", title_of(A), "护肝人")
check("B → 大唐妖探", title_of(B), "大唐妖探")
check("C → 年会不能停！", title_of(C), "年会不能停！")
check("D → 寒战1994（年份紧贴片名，不能被切掉）", title_of(D2), "寒战1994")
# ⚠️ 2026-10-05 第二轮改口径：**全广告的名字不再硬凑片名**。
#    旧版会从 `地址发布页 收藏不迷路` 里抽出 `收藏不迷路` 当片名（等于没抽）。
#    现在判不出就返回空，由 suggest() 兜底回落到原名 —— 前端表现为「无需改」。
check("全广告名 → 判不出片名（返回空）", title_of("地址发布页 收藏不迷路"), "")

print("\n=== 年份抽取 ===")
check("B 里有 2026", extract_year(B), "2026")
check("D 取到 2026（而不是片名里的 1994）", extract_year(D2), "2026")
check("A 没有年份", extract_year(A), None)

print("\n=== 顶层目录名：片名（年份）===")
check("A + 添加年份兜底", dir_name(title_of(A), "2026"), "护肝人（2026）")
check("B + 原名里的年份", dir_name(title_of(B), extract_year(B)), "大唐妖探（2026）")
check("半角括号可切换", dir_name(title_of(A), "2026", paren="half"), "护肝人(2026)")
check("系列", dir_name("年会不能停！", series=True), "年会不能停！（系列）")
check("没有年份就不加括号", dir_name("护肝人"), "护肝人")

print("\n=== suggest：候选列表 ===")
sug = suggest(A, add_year="2026")
print("  A 的候选：", [(o["mode"], o["name"]) for o in sug])
check("第一档就是 片名（年份）", sug[0]["name"], "护肝人（2026）")
check("含『保持原名』兜底", sug[-1]["name"], A)
sug_c = suggest(C, add_year="2026", series=True)
print("  C 的候选：", [(o["mode"], o["name"]) for o in sug_c])
check("C 选系列 → 年会不能停！（系列）", sug_c[0]["name"], "年会不能停！（系列）")
sug_d = suggest(D2, add_year="2026")
check("D 年份取自原名", sug_d[0]["name"], "寒战1994（2026）")

print("\n=== 分集文件名：剧名.SxxExx.第 N 集.集标题.ext ===")
check("识别 SxxExx", is_episode_file("驻院医生.S01E01.Pilot.mkv"), True)
check("E01 → 第 1 集", episode_name("驻院医生.S01E01.Pilot.mkv"), "驻院医生.S01E01.第 1 集.Pilot.mkv")
check("E07 → 第 7 集", episode_name("驻院医生.S01E07.The Elopement.mkv"), "驻院医生.S01E07.第 7 集.The Elopement.mkv")
check("字幕文件同规则", episode_name("驻院医生.S01E02.Independence Day.ass"), "驻院医生.S01E02.第 2 集.Independence Day.ass")
check("无集标题 → 剧名.SxxExx.ext", episode_name("我的媳妇.S01E30.mkv"), None)
check("幂等：已规范的不再改", episode_name("驻院医生.S01E01.第 1 集.Pilot.mkv"), None)
check("幂等：S01.E01 写法", episode_name("驻院医生.S01.E01.Pilot.mkv"), "驻院医生.S01E01.第 1 集.Pilot.mkv")
check("非分集文件不碰", episode_name("说明.txt"), None)
check("可选：用父目录片名替换前缀", episode_name("乱码.S01E05.Pilot.mkv", "驻院医生"), "驻院医生.S01E05.第 5 集.Pilot.mkv")
check("技术尾巴清掉后落回『无集标题』形式", episode_name("我的媳妇.S01E30.1080p.WEB-DL.mkv"), "我的媳妇.S01E30.mkv")
check("真集标题旁的技术标签也要清掉", episode_name("驻院医生.S01E01.Pilot.1080p.WEB-DL.mkv"), "驻院医生.S01E01.第 1 集.Pilot.mkv")

print("\n=== sanitize / ensure_ext ===")
bad = sanitize_name("年会不能停！<测试>，第二部")
check("去掉 < > 与中文逗号", ("<" not in bad and ">" not in bad and "，" not in bad), True)
check("首尾点与空格清掉", sanitize_name("  片名.  "), "片名")
check("补扩展名", ensure_ext("护肝人", "mp4"), "护肝人.mp4")
check("已有扩展名不重复", ensure_ext("护肝人.mp4", "mp4"), "护肝人.mp4")
check("目录不加扩展名", ensure_ext("护肝人", None), "护肝人")

print("\n=== clean_file：只洗主名（扩展名单拎）===")
check("整个名字是广告 → 无候选（该删不该改）",
      clean_file("【更多无水印高品质资源请访问 www.Butailing.com】.MKV"), None)
check("洗掉括号广告、保留扩展名",
      clean_file("护肝人.1080p.HD中字[最新电影www.dyg7.com].mp4"), "护肝人.1080p.HD中字.mp4")
check("已经干净 → 无候选", clean_file("驻院医生.S01E01.Pilot.mkv"), None)
check("looks_like_ad 只做提示（不做自动动作）",
      (looks_like_ad("地址发布页 收藏不迷路"), looks_like_ad("驻院医生.S01E01.Pilot.mkv")), (True, False))

print("\n=== parse_media（仅展示用）===")
info = parse_media(C)
print("  C 解析：", info)
check("年份", info.get("year"), "2023")
check("分辨率", info.get("resolution"), "2160p")
check("编码", (info.get("codec") or "").upper(), "H265")
check("压组", info.get("group"), "GPTHD")

# =====================================================================
# 第二轮（2026-10-05 用户手机截图反馈）：命名 / 清理的两类真 bug
# =====================================================================
print("\n=== 第二轮：垃圾候选（洗完全是广告残渣 ⇒ 判为无变化）===")
JUNK = "最新网址找回：www.btsj123.com 收藏不迷路.txt"
check("截图里那个 .txt 不再产出『最新网址找回：.txt』", clean_file(JUNK), None)
check("全广告名 clean 原样返回（不产出半截残渣）", clean(JUNK), JUNK)
check("★ 重叠噪声词要一次吃干净（不能先被『网址找回』截胡剩个『最新』）",
      clean("最新网址找回：abc"), "最新网址找回：abc")

print("\n=== 第二轮：站点名尾巴（BT世界网 之类不是作品名）===")
SITE = "歪心狼对阵ACME.2026.1080P.AAC.H264.CHS.BT世界网[www.btsj6.com].mp4"
check("洗掉 [网址] 括号 + BT世界网", clean_file(SITE), "歪心狼对阵ACME.2026.1080P.AAC.H264.CHS.mp4")
check("title_of 不被站名污染（旧版会得到 歪心狼对阵ACMEBT世界网）", title_of(SITE), "歪心狼对阵ACME")
check("站名 + 技术尾巴都清掉", title_of("歪心狼对阵ACME.2026.1080P.AAC.H264.CHS.BTS.J6"), "歪心狼对阵ACME")

print("\n=== 第二轮：普通文件候选（suggest_file）===")
from namer import suggest_file  # noqa: E402
SF = suggest_file(SITE)
print("  视频文件候选：", [(o["mode"], o["name"]) for o in SF])
check("视频文件默认档 = 片名（年份）.ext", SF[0]["name"], "歪心狼对阵ACME（2026）.mp4")
check("有『保持原名』兜底", SF[-1]["name"], SITE)
check("纯广告文件只有一个『保持原名』（不给改名的机会）",
      [o["mode"] for o in suggest_file(JUNK)], ["keep"])
check("分集文件走 suggest_episode（不能把集号丢了）",
      suggest_file("我的媳妇.S01E30.1080p.WEB-DL.mkv")[0]["name"], "我的媳妇.S01E30.mkv")
check("英文名不把扩展名当片名一段",
      suggest_file("Demon.Agent.2026.1080p.WEB-DL.x265.mkv")[0]["name"], "Demon Agent（2026）.mkv")
check("非视频（.txt）不给『片名（年份）』档",
      all(o["mode"] != "title" for o in suggest_file("说明文档 收藏不迷路.txt")), True)

# =====================================================================
# 第三轮（2026-10-05 红领巾拍板）：短噪声词按语义分段判定 + 冒号按语义保留
# =====================================================================
print("\n=== 第三轮：短噪声词只在『整段皆广告』时才删 ===")
# 红领巾原话：「这类词一般都是广告，也可以根据**语义**来判断」
# ⇒ 词表不收窄（收窄会漏真广告），改成按段判定。
check("★ `收藏版` 的『收藏』是名字的一部分 ⇒ 不洗（旧版会变成 国家宝藏.版）",
      clean("国家宝藏.收藏版"), "国家宝藏.收藏版")
check("★ 同上的文件版 ⇒ 无变化（不出候选）", clean_file("国家宝藏.收藏版.mkv"), None)
check("★ `收藏家` 同样要保住（旧版会变成 家.2024）", clean_file("收藏家.2024.mkv"), None)
check("★ 整段就是广告话术（`收藏`）⇒ 还是删",
      clean_file("护肝人.收藏.mkv"), "护肝人.mkv")
check("整名都是广告 ⇒ 无变化", clean_file("收藏.mkv"), None)
check("原来那条仍要成立（段 6v电影 / 收藏 / 不迷路 整段是广告）",
      clean("护肝人.6v电影 地址发布页 www.6v123.net 收藏不迷路"), "护肝人")

print("\n=== 第三轮：title_of 不能把分隔符吃掉 ===")
check("★ 分隔符分出来的两段要保住 `·`（旧版 国家宝藏收藏版）",
      title_of("国家宝藏.收藏版"), "国家宝藏.收藏版")
check("中文+数字仍要无缝（不能被加成分隔符）", title_of("寒战1994（2026）"), "寒战1994")

print("\n=== 第三轮：冒号按语义保留 ===")
# 红领巾原话：「一些冒号需要保留」。
# `：` 在广告提醒句里是噪声、在正片名里是分隔符 —— 用语义区分。
# ⚠️ 2026-10-06 第四轮改口径：正片名的冒号**逐字保留**（全角还是全角、不补空格）——
#    「全角冒号就是他习惯的写法，不要提示改」。旧版统一成 `: ` 会多出无谓的「改用」。
check("★ 正片名的冒号要留（旧版被抹成 名侦探柯南犯人犯泽先生）",
      title_of("名侦探柯南：犯人犯泽先生（2022）"), "名侦探柯南：犯人犯泽先生")
check("全角冒号原样保留（不转半角、不补空格）", clean("名侦探柯南：犯人犯泽先生（2022）"),
      "名侦探柯南：犯人犯泽先生（2022）")
check("半角冒号原本就正常，别改坏", title_of("007: 大破天幕杀机 (2012)"), "大破天幕杀机")
check("① 冒号后跟网址 ⇒ 提醒句，仍要判为无变化", clean("地址发布页：https://x.com/abc"),
      "地址发布页：https://x.com/abc")
check("② 冒号前整段是广告话术 ⇒ 仍要判为无变化", clean("最新网址找回：abc"),
      "最新网址找回：abc")
check("③ 冒号落在结尾 + 前半是正经片名 ⇒ 广告尾巴清掉、片名留下",
      clean_file("护肝人 最新网址找回：.mkv"), "护肝人.mkv")

# =====================================================================
# 第四轮（2026-10-06 红领巾口径）：习惯写法不提示改
#   原话：「全角冒号、全角半角的括号不需要报错，带系列两个字的不需要报错 ——
#          这都是我习惯的写法」「不需要提示修改名称，因为我本身就是想这样修改」
# =====================================================================
print("\n=== 第四轮：习惯写法（全角冒号 / 全半角括号 / 系列）不给『改用』===")
from namer import already_good, suggest as _suggest  # noqa: E402


def _best(name, s):
    l = [x for x in (s or []) if x["mode"] != "keep" and x["name"]]
    return l[0]["name"] if l else ""


# —— 目录：结尾就是 `（年份）` / `（系列）`（全半角都认）⇒ 无候选
for nm in ("寂静之地（2018）", "寂静之地(2018)", "寂静之地2 (2021)", "寂静之地 2（2021）",
           "寂静之地：入侵日（2024）", "密室逃生2（2021）", "寒战1994（2026）",
           "护肝人（系列）", "小黄人(系列)"):
    check(f"无需改动：{nm}", _best(nm, suggest(nm)), "")
    check(f"already_good：{nm}", already_good(nm), True)
# 「名侦探柯南：犯人犯泽先生」没有年份 / 系列标记 ⇒ 不算 already_good，
# 但 title 抽出来与原名等价 ⇒ 照样「无需改动」（上面第一行已验）。
# 若再给 add_year，会被推成 `名侦探柯南：犯人犯泽先生（2026）` —— 这是**该改**的（补年份）。
# —— 带 add_year 兜底也不该把「已是想要形态」的名字推去改名
check("系列名 + add_year 仍不给候选", _best("护肝人（系列）", suggest("护肝人（系列）", add_year="2026")), "")
check("半角括号 + add_year 仍不给候选", _best("寂静之地(2018)", suggest("寂静之地(2018)", add_year="2026")), "")
# —— 但真该改的还得改（不能一刀切全放行）
check("无年份的裸片名 + add_year ⇒ 照样给候选",
      _best("护肝人", suggest("护肝人", add_year="2026")), "护肝人（2026）")
check("带广告尾巴的『系列』名 ⇒ 照样清",
      _best("护肝人（系列） 6v电影 地址发布页", suggest("护肝人（系列） 6v电影 地址发布页", add_year="2026")),
      "护肝人（2026）")
check("全广告名照旧判不出", already_good("护肝人.6v电影 地址发布页 www.6v123.net 收藏不迷路"), False)
check("『系列电影大全』不是系列标记（不能误判已规范）", already_good("系列电影大全"), False)
# —— 续集号不被吃掉（真 bug：`寂静之地 2` 曾被抽成 `寂静之地`）
check("★ 续集号带空格也不丢", title_of("寂静之地 2（2021）"), "寂静之地2")
check("续集号紧贴也不丢", title_of("寂静之地2（2021）"), "寂静之地2")
check("中间的续集号也保住", title_of("阿凡达 2 水之道"), "阿凡达2.水之道")
check("4 位数字不会被当续集号误收", title_of("大黄蜂 2018（2026）"), "大黄蜂")
# —— 文件同口径
check("文件：已规范 ⇒ 无候选", suggest_file("寂静之地：入侵日（2024）.mp4"), [{"mode": "keep", "label": "保持原名", "name": "寂静之地：入侵日（2024）.mp4"}])
check("文件：半角括号已规范 ⇒ 无候选",
      [o["mode"] for o in suggest_file("寂静之地2 (2021).mkv")], ["keep"])
check("文件：带系列的已规范 ⇒ 无候选",
      [o["mode"] for o in suggest_file("年会不能停！（系列）.mkv")], ["keep"])

print("\n" + ("全部通过" if not fail else "失败 %d 项: %s" % (len(fail), fail)))
sys.exit(1 if fail else 0)
