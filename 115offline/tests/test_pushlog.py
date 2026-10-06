"""本地推送记录（本体推送账本）的回归测试

跑法（在项目根目录）：
    python tests/test_pushlog.py

⚠️ 逻辑住在 app/pushlog.py（纯标准库）⇒ 本测试**不需要** fastapi / p115client。

覆盖点：
  - 空账本读出来的样子
  - 最新在前（id 递增）
  - 上限：满了丢最旧的（且丢的是最旧那条，不是最新的）
  - 落盘 → 重新 load 恢复（字段保形，含 links / links_more / ok / message）
  - 单条链接截断 + 超出 MAX_LINKS 时记 links_more
  - clear 返回清掉的条数、并真的写回盘
  - **盘上文件坏了不影响 load**（记账本不许拖垮服务启动）
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from pushlog import LINK_MAX_CHARS, MAX_LINKS, PushLog, clip_link  # noqa: E402

fail = []


def check(name, got, want):
    ok = got == want
    print(("  [OK] " if ok else "  [!!] ") + name)
    if not ok:
        print("       got :", got)
        print("       want:", want)
        fail.append(name)


TMP = Path(tempfile.mkdtemp(prefix="pushlog-test-"))
FP = TMP / "pushlog.json"

print("=== 空账本 ===")
log = PushLog(FP)
check("文件不存在时 load 返回 0", log.load(), 0)
check("空账本 count", log.count(), 0)
check("空账本 items", log.items(), [])
check("空账本不建文件（没写过就别落盘）", FP.exists(), False)

print("\n=== 记一笔 + 最新在前 ===")
r1 = log.add(account_id="acc-home", account="家里 NAS", dir_id="d1", dir="115电影",
             count=2, skipped=1, ok=True, ts=1759651200,
             links=[{"url": "magnet:?xt=urn:btih:aaa", "source": "链接"},
                    {"url": "magnet:?xt=urn:btih:bbb", "source": "佛曰"}])
check("第一笔 id = 1", r1["id"], 1)
check("落盘文件出现", FP.exists(), True)
check("links 原样保留（含来源）", r1["links"][0]["source"], "链接")
check("去重条数入账", r1["skipped"], 1)

r2 = log.add(account="家里 NAS", count=1, ok=False, message="推送失败：风控", ts=1759651300)
check("第二笔 id = 2", r2["id"], 2)
check("count = 2", log.count(), 2)
check("items 最新在前", [x["id"] for x in log.items()], [2, 1])

print("\n=== 上限：满了丢最旧的 ===")
small = PushLog(TMP / "small.json", max_records=3)
for i in range(5):
    small.add(count=1, ts=1759650000 + i)
check("只留 3 条", small.count(), 3)
check("留下的是最新的 3 条（id 5/4/3），最新在前", [x["id"] for x in small.items()], [5, 4, 3])

print("\n=== 落盘 → 重新 load ===")
again = PushLog(FP)
check("恢复条数", again.load(), 2)
back = again.items()
check("顺序不变（最新在前）", [x["id"] for x in back], [2, 1])
check("账号名保形", back[0]["account"], "家里 NAS")
check("失败标记保形", back[0]["ok"], False)
check("失败原因保形", back[0]["message"], "推送失败：风控")
check("目标目录保形", back[1]["dir"], "115电影")
check("links 保形", back[1]["links"][1], {"url": "magnet:?xt=urn:btih:bbb", "source": "佛曰"})
check("续写 id 不撞（恢复后接着 3）", again.add(count=1)["id"], 3)

print("\n=== 链接截断 / 超额 ===")
long_url = "magnet:?xt=urn:btih:" + "a" * (LINK_MAX_CHARS + 200)
check("超长链接被截断", len(clip_link(long_url)), LINK_MAX_CHARS)
check("截断标记是省略号", clip_link(long_url).endswith("…"), True)
check("短链接不动", clip_link("magnet:?xt=urn:btih:abc"), "magnet:?xt=urn:btih:abc")

big = PushLog(TMP / "big.json")
many = [{"url": "magnet:?xt=urn:btih:%03d" % i, "source": "链接"} for i in range(MAX_LINKS + 7)]
rec = big.add(count=len(many), links=many)
check("单条最多存 MAX_LINKS 个链接", len(rec["links"]), MAX_LINKS)
check("余下条数记进 links_more", rec["links_more"], 7)
check("count 记的是真实条数（不是存下来的条数）", rec["count"], MAX_LINKS + 7)

print("\n=== clear ===")
# ⚠️ 注意：上面的「续写 id 不撞」是记在 `again` 这个实例上的 —— `log` 内存里还是 r1/r2 两条。
check("clear 返回清掉的条数", log.clear(), 2)
check("clear 后 count", log.count(), 0)
check("clear 后盘上也是空的", json.loads(FP.read_text("utf-8"))["items"], [])

print("\n=== 坏文件不影响启动 ===")
bad = PushLog(TMP / "bad.json")
bad.path.write_text("{ 这不是 json", "utf-8")
check("坏文件 load 返回 0 而不是抛异常", bad.load(), 0)
check("坏文件之后照样能记", bad.add(count=1)["id"], 1)

print("\n" + ("全部通过" if not fail else f"失败 {len(fail)} 项: {fail}"))
sys.exit(1 if fail else 0)
