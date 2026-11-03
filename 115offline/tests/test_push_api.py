"""分批推送的**接口级**回归测试（真起 FastAPI + 假 p115 客户端）

跑法（在项目根目录，用装了依赖的 venv）：
    python tests/test_push_api.py

⚠️ 这个测试**需要** fastapi / httpx / p115client（其余测试都是纯标准库）。
   本机 venv：~/.workbuddy/binaries/python/envs/default/Scripts/python.exe

它验的不是「算得对不对」（那在 test_pusher.py），而是接线对不对：
  - 250 条 ⇒ 真的切成 3 次请求（100/100/50），**不是**一个请求塞 250 条
  - 后台任务能跑完，进度接口能逐步往前走，最后 pushed/percent 对得上
  - 中途配额不足 ⇒ 停在第 2 批，**后面的批不再发**（数一下假客户端被调了几次）
  - 单批（≤100 条）仍走同步路径，返回老结构（ok / count）
  - 同账号并发第二个推送 ⇒ 409，且错误里带当前进度
  - 整次任务只在账本里记 **一笔**（推了 250 条，不是 3 笔）
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))

TMP = Path(tempfile.mkdtemp(prefix="push-api-test-"))
os.environ["DATA_DIR"] = str(TMP)
os.environ["ACCESS_TOKEN"] = ""
os.environ["PUSH_DELAY"] = "0"          # 测试里不等那 3 秒

(TMP / "accounts.json").write_text(json.dumps([
    {"id": "acc-t", "name": "测试号", "cookie": "UID=1; CID=2; SEID=3",
     "checked": "11-03 14:00", "default_dir": "", "default_dir_name": ""}
], ensure_ascii=False), "utf-8")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

fail = []


def check(name, got, want):
    ok = got == want
    print(("  [OK] " if ok else "  [!!] ") + name)
    if not ok:
        print("       got :", got)
        print("       want:", want)
        fail.append(name)


class FakeClient:
    """假 115：记录每次请求装了多少条；可按批号返回失败。"""

    def __init__(self, fail_at=None, error="", sleep=0.0, quota=(500, 1000)):
        self.sizes = []
        self.fail_at = fail_at       # 第几批（1 起）开始失败
        self.error = error
        self.sleep = sleep
        self.quota = quota

    def clouddownload_task_add_urls(self, payload):
        n = len([k for k in payload if k.startswith("url[")])
        self.sizes.append(n)
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail_at and len(self.sizes) >= self.fail_at:
            return {"state": False, "error_msg": self.error}
        return {"state": True, "error": "", "errno": ""}

    def clouddownload_quota_info(self):
        q, t = self.quota
        return {"state": True, "quota": q, "total": t}


def use(fake):
    server._client = lambda acc: fake          # 不碰真 115
    return fake


def wait(client, job_id, seconds=10):
    """轮询到任务结束（测试里 fake 很快，10 秒足够）。"""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/push/jobs/{job_id}")
        assert r.status_code == 200, r.text
        last = r.json()["job"]
        if last["done"]:
            return last
        time.sleep(0.05)
    raise AssertionError(f"任务没在 {seconds}s 内结束：{last}")


URLS = ["magnet:?xt=urn:btih:%04d" % i for i in range(250)]
c = TestClient(server.app)

print("=== 250 条 ⇒ 3 批（100/100/50） ===")
fake = use(FakeClient())
r = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS}).json()
check("多批走后台：accepted=True", r.get("accepted"), True)
check("条数 250", r["count"], 250)
check("批数 3", r["batches"], 3)
check("每批 100", r["chunk"], 100)
job = wait(c, r["job"]["id"])
check("假客户端被调了 3 次", len(fake.sizes), 3)
check("三批分别是 100/100/50", fake.sizes, [100, 100, 50])
check("全部推成功", job["pushed"], 250)
check("ok=True", job["ok"], True)
check("percent=100", job["percent"], 100)
check("总条数带回", job["total"], 250)
check("顺带记住离线剩余配额", job["quota_remain"], 500)

print("\n=== 进度接口：跑的过程中能拿到批次 ===")
slow = use(FakeClient(sleep=0.25))
r = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS}).json()
jid = r["job"]["id"]
mid = c.get(f"/api/push/jobs/{jid}").json()["job"]
check("刚开始 state=running", mid["state"], "running")
check("running 时 done=False", mid["done"], False)
check("running 时当前批 = 1", mid["batch"], 1)
done = wait(c, jid)
check("跑完 done=True", done["done"], True)
check("跑完 batch 收在最末批", done["batch"], 3)

print("\n=== 第 2 批配额不足 ⇒ 停，第 3 批不再发 ===")
fake2 = use(FakeClient(fail_at=2, error="离线配额不足"))
r = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS}).json()
job2 = wait(c, r["job"]["id"])
check("只发了 2 次（第 3 批没发）", len(fake2.sizes), 2)
check("state=stopped", job2["state"], "stopped")
check("ok=False", job2["ok"], False)
check("已推 100 条", job2["pushed"], 100)
check("失败 100 条", job2["failed_count"], 100)
check("summary 说清剩多少", "还有 150 条没推" in job2["summary"], True)
check("summary 带原因", "离线配额不足" in job2["summary"], True)

print("\n=== 同账号并发 ⇒ 409 并报出当前进度 ===")
use(FakeClient(sleep=0.6))
r1 = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS}).json()
r2 = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS})
check("第二个被挡下 409", r2.status_code, 409)
check("错误里带「正在推送」", "正在推送" in r2.json()["detail"], True)
check("错误里带批号", "第 1/3 批" in r2.json()["detail"], True)
wait(c, r1["job"]["id"])          # 等它跑完，别影响后面的用例

print("\n=== 单批（50 条）仍走同步路径 ===")
fake3 = use(FakeClient())
r = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS[:50]}).json()
check("同步返回，没有 accepted", r.get("accepted"), None)
check("ok=True", r["ok"], True)
check("count=50", r["count"], 50)
check("只发 1 次", fake3.sizes, [50])
check("顺带把 job 快照也给了", r["job"]["batches"], 1)

print("\n=== 超总量上限 ⇒ 直接 400（不发请求） ===")
too_many = ["ed2k://|file|f%d|1|x|/" % i for i in range(server.PUSH_MAX_TOTAL + 1)]
fake4 = use(FakeClient())
r = c.post("/api/push", json={"account_id": "acc-t", "urls": too_many})
check("超上限 400", r.status_code, 400)
check("提示里带上限数字", str(server.PUSH_MAX_TOTAL) in r.json()["detail"], True)
check("一个请求都没发", fake4.sizes, [])

print("\n=== 被限流 ⇒ 退避重试一次（成功就不算失败） ===")


class ThrottleOnce(FakeClient):
    """第 2 次调用回「过于频繁」，其余正常 —— 验的就是「退避重试」这条分支。"""

    def __init__(self):
        super().__init__()
        self.n = 0

    def clouddownload_task_add_urls(self, payload):
        self.n += 1
        self.sizes.append(len([k for k in payload if k.startswith("url[")]))
        if self.n == 2:
            return {"state": False, "error_msg": "请求过于频繁，请稍后再试"}
        return {"state": True}


fake_t = use(ThrottleOnce())
_old_backoff = server.pusher.THROTTLE_BACKOFF
server.pusher.THROTTLE_BACKOFF = 0.0        # 测试里不真等 8 秒
try:
    r = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS}).json()
    jobt = wait(c, r["job"]["id"])
finally:
    server.pusher.THROTTLE_BACKOFF = _old_backoff
check("第 2 批失败后重试了一次（共 4 次调用）", fake_t.sizes, [100, 100, 100, 50])
check("重试成功 ⇒ 250 条全推完", jobt["pushed"], 250)
check("重试成功 ⇒ ok=True（不算失败批）", jobt["ok"], True)
check("没有失败批次", jobt["failed_count"], 0)

print("\n=== 账本：整次任务只记一笔 ===")
before = c.get("/api/pushlog?limit=1").json()["count"]
use(FakeClient())
r = c.post("/api/push", json={"account_id": "acc-t", "urls": URLS}).json()
wait(c, r["job"]["id"])
after = c.get("/api/pushlog?limit=1").json()["count"]
check("3 批跑完只新增 1 笔账（不是 3 笔）", after - before, 1)
top = c.get("/api/pushlog?limit=1").json()["items"][0]
check("这一笔记的是全部 250 条", top["count"], 250)
check("message 说明分了几批", "3 批" in top["message"], True)
check("那笔是成功的", top["ok"], True)
check("账号名带上", top["account"], "测试号")
stopped = [x for x in c.get("/api/pushlog?limit=50").json()["items"]
           if "配额" in x["message"]]
check("失败那笔也在账上", len(stopped), 1)
check("失败笔只有已推的 100 条", stopped[0]["count"], 100)

print("\n=== 任务列表（切账号 / 刷页面时用来接回进度） ===")
lst = c.get("/api/push/jobs?account_id=acc-t").json()
check("能列出任务", len(lst["items"]) > 0, True)
check("最新在前", lst["items"][0]["created"] >= lst["items"][-1]["created"], True)
check("别的账号查不到", c.get("/api/push/jobs?account_id=nope").json()["items"], [])

print("\n" + ("全部通过" if not fail else f"失败 {len(fail)} 项: {fail}"))
sys.exit(1 if fail else 0)
