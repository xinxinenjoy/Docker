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
check("A", clean(A), "护肝人.6v电影")
check("C：保留 [IMAX满屏版] 这类有效信息", "[IMAX满屏版]" in clean(C), True)
check("空串原样", clean(""), "")

print("\n=== 片名抽取 title_of ===")
check("A → 护肝人", title_of(A), "护肝人")
check("B → 大唐妖探", title_of(B), "大唐妖探")
check("C → 年会不能停！", title_of(C), "年会不能停！")
check("D → 寒战1994（年份紧贴片名，不能被切掉）", title_of(D2), "寒战1994")
check("广告名兜底不产出空", title_of("地址发布页 收藏不迷路") != "", True)

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

print("\n" + ("全部通过" if not fail else "失败 %d 项: %s" % (len(fail), fail)))
sys.exit(1 if fail else 0)
