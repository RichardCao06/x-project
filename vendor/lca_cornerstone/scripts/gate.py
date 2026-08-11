#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名称图『确定性 Gate』一键封装 —— 取代 SOP workflow 里那个用 Opus 重吐全图+多轮 validate/fix 的 gate agent。

把五个既有确定性脚本串起来,全程零 LLM:
  1. materialize.py            从 workflow 的 journal.jsonl 提取 consolidate finalGraph,派生 id/边/counts
  2. reconcile.py              机械收口 D/E:identity_scope 豁免背景、升格表外刻面、派生消歧刻面、扩受控词表
  3. validate_graph.py         A–E 共 11 项硬校验
  4. build_name_graph_html.py  11/11 后渲染该行业整图 html(--no-html 跳过)
  5. build_index_html.py       重生 docs/index.html(新行业自动纳入卡片;--no-html 一起跳过)

**原子发布**:先在临时文件上跑物化+对账+校验,只有 11/11 全过才覆盖到 docs/<slug>-name-graph.json;
未过则保留临时文件供检查、绝不污染已发布的图,并退出非 0(让调用方/PM 决定是否人工修)。

实测(2026-06-09 真实 run):铝 / 塑料两条完整 run 的 consolidate 产物均经此流水线达 11/11,无需任何 LLM。
故 workflow 的 Gate 阶段应只 log 一行调用本脚本,不再派 Opus agent(省 ~$3-5/场,几乎全是缓存费)。

用法:
  python3 scripts/gate.py <journal.jsonl> <slug> "<行业显示名>"
  python3 scripts/gate.py <journal.jsonl> power "电力（…）" --out docs/power-name-graph.json --date 2026-06-09
  python3 scripts/gate.py <journal.jsonl> steel "钢铁" --keep-temp     # 调试:保留中间临时文件

退出码 0=11/11 已发布;1=未过(未发布);2=参数/物化错误。
"""
import json, sys, os, argparse, subprocess, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script, *cargs):
    return subprocess.run([sys.executable, os.path.join(HERE, script), *cargs],
                          capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser(
        description="名称图确定性 Gate(materialize→reconcile→validate,原子发布)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("journal", help="workflow 运行的 journal.jsonl 路径")
    ap.add_argument("slug", help="行业 slug(如 power/steel/aluminium)")
    ap.add_argument("industry", help="行业显示名(写入 _meta.industry)")
    ap.add_argument("--out", help="发布目标(默认 docs/<slug>-name-graph.json)")
    ap.add_argument("--date", help="_meta.generated 日期(传给 materialize.py)")
    ap.add_argument("--keep-temp", action="store_true", help="保留中间临时文件(调试)")
    ap.add_argument("--no-html", action="store_true", help="11/11 通过后不再调 build_name_graph_html 渲染(CI 跳过)")
    args = ap.parse_args()

    if not os.path.exists(args.journal):
        sys.stderr.write(f"[gate] ❌ journal 不存在: {args.journal}\n")
        return 2
    out = args.out or f"docs/{args.slug}-name-graph.json"
    tmp = tempfile.mktemp(suffix=f"-{args.slug}.json")

    # 1) 物化 → 临时文件
    mat_args = [args.journal, args.slug, args.industry, tmp]
    r = run("materialize.py", *mat_args)
    sys.stderr.write(r.stdout + r.stderr)
    if not os.path.exists(tmp):
        sys.stderr.write("[gate] ❌ 物化失败(无输出文件)\n")
        return 2
    # date 注入(materialize.py 用位置参的内部默认日期;如需指定,改写 _meta.generated)
    if args.date:
        try:
            g = json.load(open(tmp)); g["_meta"]["generated"] = args.date
            json.dump(g, open(tmp, "w"), ensure_ascii=False, indent=1)
        except Exception:
            pass

    # 2) 对账(就地修临时文件)
    r = run("reconcile.py", tmp)
    sys.stderr.write(r.stdout + r.stderr)

    # 3) 校验
    v = run("validate_graph.py", tmp)
    sys.stdout.write(v.stdout)
    sys.stderr.write(v.stderr)

    passed = (v.returncode == 0)
    if passed:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        shutil.move(tmp, out)
        sys.stderr.write(f"[gate] ✅ 11/11 通过 → 已发布 {out}\n")
        # ② 顺带渲染整图 html + ③ 重生导航 index.html(--no-html 一起跳过)
        if not args.no_html:
            out_html = out[:-5] + ".html" if out.endswith(".json") else out + ".html"
            r = run("build_name_graph_html.py", out, out_html)
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            if r.returncode == 0:
                sys.stderr.write(f"[gate] ✅ html 渲染 → {out_html}\n")
            else:
                sys.stderr.write(f"[gate] ⚠ html 渲染失败(json 已发布;可手动: python3 scripts/build_name_graph_html.py {out})\n")
            # ③ 重生 docs/index.html(本行业自动纳入卡片,不再需手动 build_index_html)
            r2 = run("build_index_html.py")
            sys.stdout.write(r2.stdout)
            sys.stderr.write(r2.stderr)
            if r2.returncode == 0:
                sys.stderr.write(f"[gate] ✅ index 重生 → docs/index.html\n")
            else:
                sys.stderr.write(f"[gate] ⚠ index 重生失败(json/html 已发布;可手动: python3 scripts/build_index_html.py)\n")
        return 0
    else:
        msg = f"[gate] ❌ 未过校验,未发布(已发布的 {out} 未被触碰)。"
        if args.keep_temp:
            msg += f" 临时文件留存: {tmp}"
        else:
            try: os.remove(tmp)
            except OSError: pass
        sys.stderr.write(msg + "\n   修法:看上方 validate 报告;A 孤儿/悬空名属图完整性问题(回 closure/评审补),D/E 一般 reconcile 已收口。\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
