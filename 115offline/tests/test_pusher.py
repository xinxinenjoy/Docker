"""分批推送逻辑的回归测试（app/pusher.py）

跑法（在项目根目录）：
    python tests/test_pusher.py

⚠️ 逻辑住在 app/pusher.py（纯标准库）⇒ 本测试**不需要** fastapi / p115client。

覆盖点：
  - 切批：1644 条 → 17 批（16×100 + 44），保序、不丢、不产生空批
  - 批大小收敛：>100 压到 100；≤0 回落 100（**不是** 1 —— 见 clamp_chunk 的注释）
  - 间隔收敛：负数 → 0，超大 → 60
  - 错误归类：配额 → fatal、频繁 → throttle、单批无效 → batch、空 → batch
  - 进度快照：running / done / stopped 三种状态下的 ok / percent / failed / summary
  - 快照必须能直接 json.dumps（前端拿它就画进度条）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from pusher import (  # noqa: E402
    DEFAULT_CHUNK,
    MAX_PER_REQUEST,
    STATE_DONE,
    STATE_RUNNING,
    STATE_STOPPED,
    PushJob,
    classify_error,
    clamp_chunk,
    clamp_delay,
    split_batches,
)

fail = []


def check(name, got, want):
    ok = got == want
    print(("  [OK] " if ok else "  [!!] ") + name)
    if not ok:
        print("       got :", got)
        print("       want:", want)
        fail.append(name)


print("=== 切批：1644 条 / 每批 100 ===")
urls = ["magnet:?xt=urn:btih:%04d" % i for i in range(1644)]
batches = split_batches(urls, 100)
check("批数 = 17", len(batches), 17)
check("前 16 批都是满的", [len(b) for b in batches[:-1]], [100] * 16)
check("最后一批 44 条", len(batches[-1]), 44)
check("总数不丢", sum(len(b) for b in batches), 1644)
check("顺序不变（首条/末条对得上）", (batches[0][0], batches[-1][-1]), (urls[0], urls[-1]))
check("批内顺序也对（第 2 批首条 = 原第 101 条）", batches[1][0], urls[100])

print("\n=== 边界 ===")
check("空列表切批 = 无批", split_batches([], 100), [])
check("空串会被丢掉（不产生空批）", split_batches(["", "a", "", "b"], 100), [["a", "b"]])
check("正好 100 条 = 1 批", len(split_batches(urls[:100], 100)), 1)
check("101 条 = 2 批", [len(b) for b in split_batches(urls[:101], 100)], [100, 1])
check("批大小超上限被压到 100", clamp_chunk(500), MAX_PER_REQUEST)
check("批大小 = 0 回落默认（不是 1！）", clamp_chunk(0), DEFAULT_CHUNK)
check("批大小 = -5 回落默认", clamp_chunk(-5), DEFAULT_CHUNK)
check("批大小认字符串", clamp_chunk("50"), 50)
check("批大小认垃圾值", clamp_chunk("abc"), DEFAULT_CHUNK)
check("间隔负数 → 0", clamp_delay(-3), 0.0)
check("间隔超大 → 60", clamp_delay(999), 60.0)
check("间隔认字符串", clamp_delay("1.5"), 1.5)

print("\n=== 错误归类 ===")
check("配额不足 → fatal", classify_error("离线配额不足"), "fatal")
check("配额（英文） → fatal", classify_error("quota exceeded"), "fatal")
check("请求过于频繁 → throttle", classify_error("请求过于频繁，请稍后再试"), "throttle")
check("Too Many Requests → throttle", classify_error("Too Many Requests"), "throttle")
check("请重新登录 → fatal", classify_error("请重新登录"), "fatal")
check("单条链接无效 → batch（只坏这一批）", classify_error("文件（夹）不存在或已经删除"), "batch")
check("空文案 → batch（当普通失败）", classify_error(""), "batch")

print("\n=== 进度：全部成功 ===")
job = PushJob(account_id="acc", account="家里", dir_name="电影",
              batches=split_batches(urls, 100), chunk=100, delay=0, skipped=2)
snap0 = job.snapshot()
check("未开始时 state=queued", snap0["state"], "queued")
check("总条数", snap0["total"], 1644)
check("批数", snap0["batches"], 17)
check("未开始 percent=0", snap0["percent"], 0)

job.start()
for i, b in enumerate(job.batches):
    job.batch_ok(len(b))
    job.tick()
    if i == 0:
        mid = job.snapshot()
check("跑完一批后 state=running", mid["state"], STATE_RUNNING)
check("跑完一批 percent≈6", mid["percent"], 6)
check("跑完一批 pushed=100", mid["pushed"], 100)
check("running 时当前批 = 2", mid["batch"], 2)
job.finish(STATE_DONE)
snap = job.snapshot()
check("全部成功 ok=True", snap["ok"], True)
check("全部成功 percent=100", snap["percent"], 100)
check("全部成功 pushed=1644", snap["pushed"], 1644)
check("done 标记", snap["done"], True)
check("summary 带总条数与批数", snap["summary"], "已推送 1644 条（17 批，每批 100 条）")
check("skipped 原样带出", snap["skipped"], 2)

print("\n=== 进度：第 5 批配额不足 → 停 ===")
job2 = PushJob(account_id="acc", batches=split_batches(urls, 100), chunk=100, delay=0)
job2.start()
for i in range(4):
    job2.batch_ok(100)
    job2.tick()
job2.batch_failed(4, 100, "离线配额不足")
job2.tick()
job2.finish(STATE_STOPPED)
s2 = job2.snapshot()
check("中途失败 ok=False", s2["ok"], False)
check("state=stopped", s2["state"], STATE_STOPPED)
check("已推 400 条", s2["pushed"], 400)
check("失败条数 100", s2["failed_count"], 100)
check("失败批次号是 5（1 起）", s2["failed"][0]["batch"], 5)
check("percent 只走到 5/17", s2["percent"], 29)
check("summary 交代剩多少", "还有 1244 条没推" in s2["summary"], True)
check("summary 带 115 的原因", "离线配额不足" in s2["summary"], True)

print("\n=== 快照可直接 JSON 化（前端就靠它） ===")
try:
    dumped = json.dumps(s2, ensure_ascii=False)
    check("json.dumps 不炸", "离线配额不足" in dumped, True)
except Exception as exc:                                    # noqa: BLE001
    check("json.dumps 不炸", f"异常：{exc}", True)

print("\n" + ("全部通过" if not fail else f"失败 {len(fail)} 项: {fail}"))
sys.exit(1 if fail else 0)
