#!/usr/bin/env python3
"""放量信号回测: 5m quote_volume >= 10x 前30天(8640根)滚动均量。
数据来自本地 kl/*.zip (binance.vision futures/um 5m)。
输出: 触发事件(聚合连续bar) -> events.csv + 控制台摘要。
"""
import zipfile, glob, os, csv, datetime
from collections import defaultdict

KL_DIR = "kl"
WIN = 8640                 # 30天 * 288根/天
MULT = 10.0               # 阈值倍数
GAP_BARS = 6             # 事件聚合: 相邻触发<=6根(30min)算同一事件
BAR_MS = 5 * 60 * 1000
# 信号窗口 [07-03 00:00, 07-09 23:55] UTC
def ums(y,m,d,H=0,M=0):
    return int(datetime.datetime(y,m,d,H,M,tzinfo=datetime.timezone.utc).timestamp()*1000)
SIG_START = ums(2026,7,3)
SIG_END   = ums(2026,7,9,23,55)

def utc(ms):
    return datetime.datetime.utcfromtimestamp(ms/1000).strftime("%Y-%m-%d %H:%M")

def load_symbol(sym):
    """合并该 symbol 所有 zip -> 按 open_time 排序去重的 [(ot, qv)]。"""
    rows = {}
    for f in glob.glob(f"{KL_DIR}/{sym}-*.zip"):
        try:
            z = zipfile.ZipFile(f)
            data = z.read(z.namelist()[0]).decode()
        except Exception:
            continue
        for line in data.splitlines():
            if not line or line[0].isalpha():
                continue
            p = line.split(",")
            try:
                ot = int(p[0]); qv = float(p[7])
            except (ValueError, IndexError):
                continue
            rows[ot] = qv
    return sorted(rows.items())

def analyze(sym):
    series = load_symbol(sym)
    n = len(series)
    if n < WIN + 1:
        return []
    ot = [x[0] for x in series]
    qv = [x[1] for x in series]
    # 前缀和用于 O(1) 窗口均值
    pref = [0.0]*(n+1)
    for i in range(n):
        pref[i+1] = pref[i] + qv[i]
    hits = []
    for i in range(WIN, n):
        t = ot[i]
        if t < SIG_START or t > SIG_END:
            continue
        # 连续性: 前8640根需覆盖≈30天(容忍1根缺失换算), 否则窗口跨缺口不可信
        span = ot[i] - ot[i-WIN]
        if span > int(WIN * BAR_MS * 1.02):
            continue
        base = (pref[i] - pref[i-WIN]) / WIN     # 前30天均量(不含当前)
        if base <= 0:
            continue
        ratio = qv[i] / base
        if ratio >= MULT:
            hits.append((t, qv[i], base, ratio))
    return hits

def aggregate(sym, hits):
    """相邻触发(<=GAP_BARS)聚合成事件。"""
    if not hits:
        return []
    events = []
    cur = [hits[0]]
    for h in hits[1:]:
        if h[0] - cur[-1][0] <= GAP_BARS * BAR_MS:
            cur.append(h)
        else:
            events.append(_mk(sym, cur)); cur = [h]
    events.append(_mk(sym, cur))
    return events

def _mk(sym, grp):
    peak = max(grp, key=lambda x: x[3])
    return {
        "symbol": sym,
        "start": grp[0][0], "end": grp[-1][0],
        "n_bars": len(grp),
        "peak_time": peak[0], "peak_ratio": peak[3],
        "peak_qv": peak[1], "baseline": peak[2],
    }

def main():
    syms = sorted({os.path.basename(f).rsplit("-",1)[0].rsplit("-2026",1)[0]
                   for f in glob.glob(f"{KL_DIR}/*.zip")})
    # 更稳: 从 symbols.txt 取
    if os.path.exists("symbols.txt"):
        syms = [s for s in open("symbols.txt").read().split() if s]
    all_events = []
    covered = 0
    for s in syms:
        hits = analyze(s)
        if hits:
            all_events.extend(aggregate(s, hits))
        # 判断是否有足够历史参与(至少一个信号窗口bar被评估过)
    # 排序: 峰值倍数降序
    all_events.sort(key=lambda e: e["peak_ratio"], reverse=True)

    with open("events.csv","w",newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol","peak_time_utc","peak_ratio","peak_qv_usdt",
                    "baseline_qv_usdt_5m","n_bars","start_utc","end_utc"])
        for e in all_events:
            w.writerow([e["symbol"], utc(e["peak_time"]), f'{e["peak_ratio"]:.1f}',
                        f'{e["peak_qv"]:.0f}', f'{e["baseline"]:.0f}', e["n_bars"],
                        utc(e["start"]), utc(e["end"])])

    # ---- 摘要 ----
    tot = len(all_events)
    n_syms = len({e["symbol"] for e in all_events})
    print(f"\n===== 放量信号回测结果 (07-03 ~ 07-09, 10x/30天) =====")
    print(f"触发事件总数: {tot}   涉及币种: {n_syms}")

    # 流动性分层(baseline = 平均每5min USDT成交额)
    tiers = [("僵尸/微型  <1万/5m", 0, 1e4),
             ("小盘  1万-10万/5m", 1e4, 1e5),
             ("中盘  10万-50万/5m", 1e5, 5e5),
             ("大盘  >50万/5m", 5e5, float("inf"))]
    print("\n-- 按基准流动性分层 --")
    for name, lo, hi in tiers:
        grp = [e for e in all_events if lo <= e["baseline"] < hi]
        print(f"  {name:20s}: {len(grp):4d} 事件, {len({e['symbol'] for e in grp}):3d} 币")

    # 倍数分档
    print("\n-- 按峰值倍数分档 --")
    for name, lo, hi in [("10-20x",10,20),("20-50x",20,50),("50-100x",50,100),(">100x",100,1e9)]:
        grp=[e for e in all_events if lo<=e["peak_ratio"]<hi]
        print(f"  {name:8s}: {len(grp):4d}")

    # 每天分布
    print("\n-- 按日期(UTC)分布 --")
    byday=defaultdict(int)
    for e in all_events:
        byday[utc(e["peak_time"])[:10]] += 1
    for d in sorted(byday):
        print(f"  {d}: {byday[d]}")

    # 高质量样本: baseline>=10万/5m(有真实流动性) 的前25个
    print("\n-- 有真实流动性(基准>=10万USDT/5m)的触发, 按倍数Top25 --")
    q = [e for e in all_events if e["baseline"]>=1e5][:25]
    print(f"{'symbol':16s}{'peak_utc':18s}{'倍数':>7s}{'峰值额$':>14s}{'基准$/5m':>13s}{'持续':>5s}")
    for e in q:
        print(f"{e['symbol']:16s}{utc(e['peak_time']):18s}{e['peak_ratio']:7.1f}"
              f"{e['peak_qv']:14.0f}{e['baseline']:13.0f}{e['n_bars']:5d}")
    print(f"\n完整事件表 -> events.csv ({tot} 行)")

if __name__ == "__main__":
    main()
