#!/usr/bin/env python3
"""Build the browsable documentation center and Markdown HTML mirrors."""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import markdown
except ImportError as exc:  # pragma: no cover - exercised by the command-line environment
    raise SystemExit("Documentation build requires `pip install -e '.[docs]'`.") from exc


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


@dataclass(frozen=True)
class Document:
    category: str
    title: str
    summary: str
    source: str
    html_path: str | None = None
    generated: bool = False
    kind: str = "guide"


CATEGORIES = {
    "architecture": ("01", "架构与治理", "平台为什么这样设计，以及如何验证设计没有被工作流局部规则架空。"),
    "delivery": ("02", "实施与运行", "当前实现边界、长任务治理、上线状态和剩余准入条件。"),
    "wiki": ("03", "Wiki 生产与验收", "从垂直切片到 Golden Case 的可追溯生产证据与复盘。"),
    "machine": ("04", "机器可读清单", "迁移来源、文件哈希和 Phase 2 资产映射。"),
}


DOCUMENTS = [
    Document(
        "architecture", "骨架数据库自治生产平台 · 技术设计",
        "平台控制面、能力边界、状态机、发布事务和工程重构的权威技术设计。",
        "技术设计-骨架数据库自治生产平台.html",
        "技术设计-骨架数据库自治生产平台.html", kind="design",
    ),
    Document(
        "architecture", "系统自我修复与目标对齐架构",
        "Goal Contract、三层闭环、Meta Supervisor、复合动作图与效果证明合同。",
        "系统自我修复与目标对齐架构.md",
        "系统自我修复与目标对齐架构.html", generated=True, kind="design",
    ),
    Document(
        "architecture", "Goal Contract Governance v2",
        "三合同、Capability Envelope、Goal 修正案、自治资格与受治理发布的实现边界。",
        "goal-contract-governance-v2.md",
        "goal-contract-governance-v2.html", generated=True, kind="design",
    ),
    Document(
        "architecture", "研究约束治理重设计 v1",
        "区分阻断契约与质量目标，并以稳定问题契约和逐问题证据闭合替代关键词启发式。",
        "research-constraint-governance-redesign-v1.html",
        "research-constraint-governance-redesign-v1.html", kind="design",
    ),
    Document(
        "architecture", "Job 跨阶段状态一致性与自主收敛设计 v1",
        "定义 Job/Run/Task/Item/Campaign 原子恢复、Artifact 代际、唯一 Repair Graph、差分 Canary 与最终发布收尾。",
        "job-execution-consistency-autonomous-convergence-v1.md",
        "job-execution-consistency-autonomous-convergence-v1.html", generated=True, kind="design",
    ),
    Document(
        "architecture", "自治修复的受控 SCM 发布",
        "偏差 Issue、隔离分支、可追踪 commit、Draft PR、失败降级与基线一致性保护。",
        "system-repair-scm.md",
        "system-repair-scm.html", generated=True, kind="design",
    ),
    Document(
        "architecture", "自治生产平台 · 测试策略与测试用例",
        "测试矩阵、优先级、Mutation、Golden、Shadow、Canary 与验收证据要求。",
        "测试设计-骨架数据库自治生产平台.html",
        "测试设计-骨架数据库自治生产平台.html", kind="test",
    ),
    Document(
        "delivery", "重构实施与验收状态",
        "已完成能力、可复现测试证据、尚未通过的验收范围和下一里程碑。",
        "重构实施与验收状态.md",
        "重构实施与验收状态.html", generated=True, kind="status",
    ),
    Document(
        "delivery", "A001 Wiki 生产长任务优化与修复方案",
        "长任务基准、状态失真、Provider 性能、重试预算和 Supervisor 治理方案。",
        "A001-Wiki生产长任务优化与修复方案.md",
        "A001-Wiki生产长任务优化与修复方案.html", generated=True, kind="runbook",
    ),
    Document(
        "wiki", "P003 节点 Wiki 全流程复盘与 Golden Case 工作流",
        "从早期 Draft Gate 到高质量 Golden 的完整时间线、失效模式和目标工作流。",
        "P003节点Wiki生产全流程复盘与Golden-Case工作流.md",
        "P003节点Wiki生产全流程复盘与Golden-Case工作流.html", kind="retrospective",
    ),
    Document(
        "wiki", "P003 Golden 内容优先重跑验收报告",
        "内容优先策略的 v26/v27 对照、质量指标、修复记录与正式产物。",
        "P003-Golden内容优先重跑验收报告.md",
        "P003-Golden内容优先重跑验收报告.html", generated=True, kind="acceptance",
    ),
    Document(
        "wiki", "Phase 2 · Wiki 垂直切片验收报告",
        "A017、P031、P003 三节点的隔离演练、Gate 结果和未发布原因。",
        "Phase2-Wiki垂直切片验收报告.md",
        "Phase2-Wiki垂直切片验收报告.html", generated=True, kind="acceptance",
    ),
    Document(
        "wiki", "Phase 3 · Wiki 真实证据正式发布验收报告",
        "真实证据、独立核验、发布门禁、工程资产与源仓库保护的验收记录。",
        "Phase3-Wiki真实证据正式发布验收报告.md",
        "Phase3-Wiki真实证据正式发布验收报告.html", generated=True, kind="acceptance",
    ),
    Document(
        "machine", "迁移资产清单",
        "从只读来源迁移到当前项目的资产、来源路径和内容哈希。",
        "migration-manifest.json", kind="manifest",
    ),
    Document(
        "machine", "Wiki Phase 2 迁移清单",
        "Wiki Phase 2 专项资产、来源映射和迁移完整性记录。",
        "wiki-phase2-migration-manifest.json", kind="manifest",
    ),
]


KIND_LABELS = {
    "design": "设计",
    "test": "测试",
    "status": "状态",
    "runbook": "运行方案",
    "retrospective": "复盘",
    "acceptance": "验收",
    "manifest": "清单",
}


DOCUMENT_STYLE = r"""
    :root {
      --ink: #132321; --muted: #60706c; --paper: #f2eee4; --panel: #fffdf7;
      --line: #c9c2b2; --teal: #006f67; --teal-soft: #d9ebe5; --rust: #c54e2f;
      --navy: #102b2d; --code: #102a2b; --code-ink: #e7f0e8;
      --serif: "Iowan Old Style", "Songti SC", "STSong", Georgia, serif;
      --sans: "Avenir Next", "PingFang SC", "Hiragino Sans GB", sans-serif;
      --mono: "IBM Plex Mono", "SFMono-Regular", Menlo, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body { margin: 0; overflow-x: hidden; color: var(--ink); background:
      linear-gradient(90deg, rgba(16,43,45,.035) 1px, transparent 1px) 0 0/34px 34px,
      linear-gradient(rgba(16,43,45,.035) 1px, transparent 1px) 0 0/34px 34px, var(--paper);
      font: 16px/1.78 var(--sans); }
    a { color: var(--teal); text-underline-offset: .2em; }
    .topbar { position: sticky; top: 0; z-index: 20; display: flex; align-items: center;
      justify-content: space-between; gap: 1rem; min-height: 58px; padding: .7rem clamp(1rem,4vw,3.5rem);
      color: #eff8f2; background: rgba(16,43,45,.96); border-bottom: 3px solid #d98b45;
      backdrop-filter: blur(12px); }
    .topbar a { color: inherit; text-decoration: none; }
    .brand { display: flex; align-items: center; gap: .7rem; font-weight: 700; letter-spacing: .04em; }
    .brand-mark { display: grid; place-items: center; width: 28px; height: 28px; border: 1px solid #8acbc1;
      border-radius: 50%; color: #f2bd72; font: 700 12px var(--mono); }
    .source-link { padding: .32rem .7rem; border: 1px solid rgba(255,255,255,.3); border-radius: 999px;
      font: 12px var(--mono); }
    .shell { display: grid; grid-template-columns: minmax(220px, 290px) minmax(0, 850px); gap: clamp(2rem,5vw,5rem);
      max-width: 1260px; margin: 0 auto; padding: clamp(2rem,5vw,5rem) clamp(1rem,4vw,3.5rem) 6rem; }
    .toc { position: sticky; top: 90px; align-self: start; max-height: calc(100vh - 120px); overflow: auto;
      padding: 1rem 1rem 1.2rem; background: rgba(255,253,247,.76); border: 1px solid var(--line);
      box-shadow: 8px 8px 0 rgba(16,43,45,.07); }
    .toc-label { margin: 0 0 .8rem; color: var(--rust); font: 700 11px var(--mono); letter-spacing: .16em; }
    .toc ul { margin: 0; padding-left: 1rem; list-style: none; }
    .toc > ul { padding-left: 0; }
    .toc li { margin: .25rem 0; }
    .toc a { display: block; padding: .22rem .35rem; color: #41534f; border-left: 2px solid transparent;
      font-size: .82rem; line-height: 1.45; text-decoration: none; }
    .toc a:hover { color: var(--teal); border-left-color: var(--rust); background: #fff; }
    .document { min-width: 0; }
    .document-meta { display: flex; flex-wrap: wrap; gap: .55rem; margin: 0 0 2rem; }
    .chip { padding: .25rem .58rem; color: var(--teal); background: var(--teal-soft); border-radius: 999px;
      font: 700 11px var(--mono); letter-spacing: .06em; }
    article { padding: clamp(1.4rem,4vw,4rem); overflow-wrap: anywhere; background: rgba(255,253,247,.92); border: 1px solid var(--line);
      box-shadow: 18px 22px 0 rgba(16,43,45,.07); }
    h1, h2, h3, h4 { font-family: var(--serif); line-height: 1.24; text-wrap: balance; }
    h1 { max-width: 17ch; margin: 0 0 1.8rem; font-size: clamp(2.25rem,6vw,4.7rem); letter-spacing: -.045em; }
    h2 { margin: 3.5rem 0 1rem; padding-top: .75rem; border-top: 2px solid var(--navy);
      font-size: clamp(1.55rem,3vw,2.2rem); }
    h3 { margin-top: 2.4rem; color: #194c4a; font-size: 1.35rem; }
    h4 { color: #344c48; }
    .headerlink { margin-left: .35em; color: var(--teal); opacity: 0; font: .45em var(--mono); text-decoration: none; }
    h1:hover .headerlink, h2:hover .headerlink, h3:hover .headerlink, h4:hover .headerlink,
    .headerlink:focus { opacity: .65; }
    p, li { max-width: 78ch; }
    strong { color: #0c4b47; }
    blockquote { margin: 1.5rem 0; padding: .8rem 1.2rem; color: #374b47; background: #edf2e9;
      border-left: 4px solid var(--rust); }
    code { padding: .12em .34em; color: #8c311e; background: #eee5d8; border-radius: 3px; font: .88em var(--mono); }
    pre { max-width: 100%; overflow: auto; padding: 1.2rem; color: var(--code-ink); background: var(--code);
      border-left: 4px solid #d98b45; box-shadow: inset 0 0 0 1px rgba(255,255,255,.08); }
    pre code { padding: 0; color: inherit; background: transparent; }
    pre.mermaid { color: var(--ink); background: #f7f3e9; border: 1px solid var(--line); border-left: 4px solid var(--teal); }
    .table-wrap { width: 100%; overflow-x: auto; margin: 1.4rem 0; }
    table { width: 100%; border-collapse: collapse; font-size: .9rem; }
    th { color: #f5f7ef; background: var(--navy); text-align: left; }
    th, td { padding: .65rem .75rem; border: 1px solid #bbb6aa; vertical-align: top; }
    tr:nth-child(even) td { background: #f4f0e7; }
    hr { margin: 3rem 0; border: 0; border-top: 1px solid var(--line); }
    .footer { max-width: 1260px; margin: 0 auto; padding: 0 3.5rem 3rem; color: var(--muted); font-size: .8rem; }
    @media (max-width: 860px) {
      .shell { display: block; padding-top: 1.5rem; }
      .toc { position: relative; top: auto; max-height: 18rem; margin-bottom: 1.5rem; }
      article { padding: 1.25rem; box-shadow: 8px 10px 0 rgba(16,43,45,.07); }
      h1 { font-size: clamp(2rem,12vw,3.25rem); }
      .topbar { align-items: flex-start; }
    }
    @media print { .topbar, .toc { display: none; } .shell { display: block; max-width: none; padding: 0; }
      article { border: 0; box-shadow: none; } body { background: #fff; } }
"""


def markdown_to_html(document: Document) -> str:
    source_path = DOCS / document.source
    source = source_path.read_text(encoding="utf-8")
    renderer = markdown.Markdown(
        extensions=["extra", "sane_lists", "toc"],
        extension_configs={"toc": {"permalink": "§", "toc_depth": "2-4"}},
        output_format="html5",
    )
    body = renderer.convert(source)
    toc = renderer.toc or '<p class="toc-empty">本文档没有章节目录。</p>'
    body = re.sub(
        r'<pre><code class="language-mermaid">(.*?)</code></pre>',
        r'<pre class="mermaid">\1</pre>', body, flags=re.DOTALL,
    )
    body = re.sub(r"<table>(.*?)</table>", r'<div class="table-wrap"><table>\1</table></div>', body, flags=re.DOTALL)
    markdown_html = {d.source: d.html_path for d in DOCUMENTS if d.source.endswith(".md") and d.html_path}
    for source_name, html_path in markdown_html.items():
        body = body.replace(f'href="{html.escape(source_name)}"', f'href="{html.escape(html_path or source_name)}"')
    title = html.escape(document.title)
    source_link = html.escape(document.source)
    kind = html.escape(KIND_LABELS[document.kind])
    mermaid = """
  <script type="module">
    import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
    mermaid.initialize({ startOnLoad: true, theme: 'neutral', securityLevel: 'loose' });
  </script>""" if "class=\"mermaid\"" in body else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="description" content="{html.escape(document.summary)}">
  <title>{title} · LCA 项目文档</title>
  <style>{DOCUMENT_STYLE}</style>
</head>
<body>
  <!-- Generated by scripts/build_docs_site.py; edit the Markdown source, not this file. -->
  <header class="topbar">
    <a class="brand" href="index.html"><span class="brand-mark">LCA</span><span>项目文档中心</span></a>
    <a class="source-link" href="{source_link}">查看 Markdown 源文档</a>
  </header>
  <main class="shell">
    <nav class="toc" aria-label="本文目录"><p class="toc-label">CONTENTS / 目录</p>{toc}</nav>
    <section class="document">
      <div class="document-meta"><span class="chip">{kind}</span><span class="chip">HTML MIRROR</span></div>
      <article>{body}</article>
    </section>
  </main>
  <footer class="footer">本页由 <code>scripts/build_docs_site.py</code> 从权威 Markdown 自动生成。</footer>
{mermaid}
</body>
</html>
"""


def entry_url(document: Document) -> str:
    return document.html_path or document.source


def source_links(document: Document) -> str:
    items = []
    if document.source.endswith(".md"):
        items.append(f'<a href="{html.escape(document.source)}">Markdown</a>')
    elif document.source.endswith(".json"):
        items.append(f'<a href="{html.escape(document.source)}">JSON</a>')
    if document.html_path:
        items.insert(0, f'<a class="primary" href="{html.escape(document.html_path)}">阅读 HTML</a>')
    return "".join(items)


def build_index() -> str:
    groups = []
    for category, (number, title, summary) in CATEGORIES.items():
        cards = []
        for document in [item for item in DOCUMENTS if item.category == category]:
            badge = KIND_LABELS[document.kind]
            cards.append(f"""
          <article class="doc-card" data-search="{html.escape((document.title + ' ' + document.summary + ' ' + badge).lower())}">
            <div class="card-kicker"><span>{html.escape(badge)}</span><span>{html.escape(document.source.rsplit('.', 1)[-1].upper())}</span></div>
            <h3><a href="{html.escape(entry_url(document))}">{html.escape(document.title)}</a></h3>
            <p>{html.escape(document.summary)}</p>
            <div class="card-links">{source_links(document)}</div>
          </article>""")
        groups.append(f"""
      <section class="doc-group" id="{category}">
        <div class="group-intro"><span class="group-number">{number}</span><div><h2>{html.escape(title)}</h2><p>{html.escape(summary)}</p></div></div>
        <div class="doc-grid">{''.join(cards)}</div>
      </section>""")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="description" content="LCA Skeleton Autonomous Production Platform 统一文档入口">
  <title>LCA 项目文档中心</title>
  <style>
    :root {{ --ink:#122321; --paper:#eee9de; --panel:#fffdf7; --line:#c8c0ae; --teal:#006f67;
      --navy:#102b2d; --rust:#c54e2f; --gold:#dc974c; --muted:#61706c;
      --serif:"Iowan Old Style","Songti SC","STSong",Georgia,serif;
      --sans:"Avenir Next","PingFang SC","Hiragino Sans GB",sans-serif;
      --mono:"IBM Plex Mono","SFMono-Regular",Menlo,monospace; }}
    * {{ box-sizing:border-box; }} html {{ scroll-behavior:smooth; }}
    body {{ margin:0; color:var(--ink); background:var(--paper); font:16px/1.65 var(--sans); }}
    body::before {{ content:""; position:fixed; inset:0; z-index:-1; opacity:.34; background:
      radial-gradient(circle at 16% 8%, rgba(220,151,76,.28), transparent 28%),
      linear-gradient(90deg, rgba(16,43,45,.055) 1px, transparent 1px) 0 0/42px 42px,
      linear-gradient(rgba(16,43,45,.055) 1px, transparent 1px) 0 0/42px 42px; }}
    a {{ color:inherit; }}
    .masthead {{ min-height:70vh; display:grid; grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr);
      gap:clamp(2rem,7vw,7rem); align-items:end; padding:clamp(2rem,7vw,7rem); color:#edf7ef; background:var(--navy);
      border-bottom:8px solid var(--gold); overflow:hidden; position:relative; }}
    .masthead::after {{ content:"LCA"; position:absolute; right:-.08em; top:-.18em; color:rgba(255,255,255,.035);
      font:900 clamp(13rem,37vw,36rem)/1 var(--serif); letter-spacing:-.1em; pointer-events:none; }}
    .eyebrow {{ margin:0 0 1rem; color:#8ed4c9; font:700 12px var(--mono); letter-spacing:.18em; }}
    h1,h2,h3 {{ font-family:var(--serif); line-height:1.12; text-wrap:balance; }}
    h1 {{ max-width:12ch; margin:0; font-size:clamp(3rem,9vw,8.2rem); letter-spacing:-.065em; }}
    .lead {{ max-width:34rem; margin:0 0 1rem; color:#c8d8d2; font-size:clamp(1rem,2.2vw,1.35rem); }}
    .stat {{ display:flex; gap:.7rem; align-items:baseline; margin:1.6rem 0 0; color:#8ed4c9; font:12px var(--mono); }}
    .stat strong {{ color:#f3b96f; font:700 2.6rem var(--serif); }}
    .search-shell {{ position:sticky; top:0; z-index:10; display:flex; gap:1rem; align-items:center;
      padding:1rem clamp(1rem,7vw,7rem); background:rgba(238,233,222,.93); border-bottom:1px solid var(--line); backdrop-filter:blur(14px); }}
    .search-shell label {{ color:var(--rust); font:700 11px var(--mono); letter-spacing:.14em; }}
    #search {{ flex:1; min-width:0; padding:.8rem 1rem; color:var(--ink); background:var(--panel); border:1px solid var(--navy);
      border-radius:0; font:inherit; outline:none; }} #search:focus {{ box-shadow:0 0 0 3px rgba(0,111,103,.18); }}
    .content {{ max-width:1480px; margin:auto; padding:clamp(3rem,7vw,7rem); }}
    .doc-group {{ margin:0 0 clamp(5rem,10vw,10rem); scroll-margin-top:90px; }}
    .group-intro {{ display:grid; grid-template-columns:7rem minmax(0,42rem); gap:1.2rem; align-items:start; margin-bottom:2rem; }}
    .group-number {{ color:var(--rust); border-top:4px solid var(--rust); padding-top:.35rem; font:700 2rem var(--mono); }}
    .group-intro h2 {{ margin:0; font-size:clamp(2rem,5vw,4rem); letter-spacing:-.04em; }}
    .group-intro p {{ color:var(--muted); }}
    .doc-grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:1.25rem; }}
    .doc-card {{ grid-column:span 4; display:flex; flex-direction:column; min-height:300px; padding:1.4rem;
      background:rgba(255,253,247,.92); border:1px solid var(--line); box-shadow:10px 12px 0 rgba(16,43,45,.08);
      transition:transform .2s ease, box-shadow .2s ease; animation:rise .5s both; }}
    .doc-card:nth-child(2) {{ animation-delay:.06s; }} .doc-card:nth-child(3) {{ animation-delay:.12s; }}
    .doc-card:hover {{ transform:translate(-3px,-4px); box-shadow:15px 18px 0 rgba(16,43,45,.11); }}
    .card-kicker {{ display:flex; justify-content:space-between; color:var(--rust); font:700 10px var(--mono); letter-spacing:.12em; }}
    .doc-card h3 {{ margin:2.2rem 0 .8rem; font-size:clamp(1.35rem,2.5vw,2rem); }}
    .doc-card h3 a {{ text-decoration:none; }} .doc-card h3 a:hover {{ color:var(--teal); }}
    .doc-card p {{ margin:0 0 2rem; color:var(--muted); }}
    .card-links {{ display:flex; flex-wrap:wrap; gap:.55rem; margin-top:auto; }}
    .card-links a {{ padding:.38rem .64rem; color:var(--teal); border:1px solid #8aa39c; text-decoration:none; font:700 11px var(--mono); }}
    .card-links a.primary {{ color:#fff; background:var(--teal); border-color:var(--teal); }}
    .doc-card.hidden {{ display:none; }}
    .empty {{ display:none; padding:4rem; text-align:center; border:1px dashed var(--line); }}
    .footer {{ padding:2rem clamp(1rem,7vw,7rem) 4rem; color:#c5d3ce; background:var(--navy); }}
    .footer code {{ color:#f3b96f; font-family:var(--mono); }}
    @keyframes rise {{ from {{ opacity:0; transform:translateY(14px); }} to {{ opacity:1; transform:none; }} }}
    @media (max-width:960px) {{ .masthead {{ min-height:auto; grid-template-columns:1fr; padding:4rem 1.2rem; }}
      .doc-card {{ grid-column:span 6; }} .content {{ padding:3rem 1.2rem; }} }}
    @media (max-width:620px) {{ .search-shell label {{ display:none; }} .group-intro {{ grid-template-columns:3.2rem 1fr; }}
      .doc-card {{ grid-column:1/-1; min-height:250px; }} }}
    @media (prefers-reduced-motion:reduce) {{ * {{ animation:none!important; transition:none!important; }} }}
  </style>
</head>
<body>
  <header class="masthead">
    <div><p class="eyebrow">LCA SKELETON AUTONOMOUS PRODUCTION</p><h1>项目文档中心</h1></div>
    <div><p class="lead">统一浏览架构设计、自治修复、实施状态、Wiki Golden Case 与验收证据。Markdown 是正文来源，HTML 是阅读镜像。</p>
      <p class="stat"><strong>{len(DOCUMENTS)}</strong><span>份已编目文档</span></p></div>
  </header>
  <div class="search-shell"><label for="search">FILTER / 筛选</label><input id="search" type="search" placeholder="搜索标题、主题或文档类型…" autocomplete="off"></div>
  <main class="content">{''.join(groups)}<p class="empty" id="empty">没有匹配的文档。</p></main>
  <footer class="footer">入口由 <code>scripts/build_docs_site.py</code> 生成。修改 Markdown 后运行 <code>python scripts/build_docs_site.py</code> 同步 HTML。</footer>
  <script>
    const input=document.getElementById('search'), cards=[...document.querySelectorAll('.doc-card')], empty=document.getElementById('empty');
    input.addEventListener('input',()=>{{const q=input.value.trim().toLowerCase();let count=0;cards.forEach(card=>{{const show=!q||card.dataset.search.includes(q);card.classList.toggle('hidden',!show);if(show)count++;}});empty.style.display=count?'none':'block';}});
  </script>
</body>
</html>
"""


def build_readme() -> str:
    lines = [
        "# 项目文档中心", "",
        "这里是 `lca-project` 的统一文档入口。优先使用 [HTML 文档中心](index.html) 浏览；",
        "需要审阅、引用或修改正文时，使用对应 Markdown 源文件。", "",
        "> 维护规则：Markdown 是正文的权威来源，同名 HTML 是阅读镜像。修改 Markdown 后运行",
        "> `python scripts/build_docs_site.py`；手工设计的技术设计、测试设计与 P003 全流程 HTML 不会被覆盖。", "",
    ]
    for category, (number, title, summary) in CATEGORIES.items():
        lines.extend([f"## {number} · {title}", "", summary, "", "| 文档 | 阅读入口 | 源文件 | 说明 |", "|---|---|---|---|"])
        for document in [item for item in DOCUMENTS if item.category == category]:
            html_link = f"[HTML]({document.html_path})" if document.html_path else "—"
            source_label = "Markdown" if document.source.endswith(".md") else "JSON" if document.source.endswith(".json") else "HTML"
            source_link = f"[{source_label}]({document.source})"
            lines.append(f"| {document.title} | {html_link} | {source_link} | {document.summary} |")
        lines.append("")
    lines.extend([
        "## 生成与校验", "", "```bash", "pip install -e '.[docs]'", "python scripts/build_docs_site.py --check", "python scripts/build_docs_site.py", "```", "",
        "`--check` 会验证所有人读 Markdown/HTML 是否已进入目录，并检查已生成 HTML 是否与源文件同步。",
    ])
    return "\n".join(lines) + "\n"


def expected_outputs() -> dict[Path, str]:
    outputs = {DOCS / "index.html": build_index(), DOCS / "README.md": build_readme()}
    for document in DOCUMENTS:
        if document.generated and document.html_path:
            outputs[DOCS / document.html_path] = markdown_to_html(document)
    return outputs


def validate_catalog() -> list[str]:
    errors = []
    catalog_sources = {document.source for document in DOCUMENTS}
    catalog_html = {document.html_path for document in DOCUMENTS if document.html_path}
    markdown_files = {path.name for path in DOCS.glob("*.md")} - {"README.md"}
    html_files = {path.name for path in DOCS.glob("*.html")} - {"index.html"}
    for missing in sorted(markdown_files - catalog_sources):
        errors.append(f"Markdown is missing from documentation catalog: {missing}")
    for missing in sorted(html_files - catalog_html):
        errors.append(f"HTML is missing from documentation catalog: {missing}")
    for document in DOCUMENTS:
        if not (DOCS / document.source).is_file():
            errors.append(f"Catalog source does not exist: {document.source}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when catalog or generated pages are stale")
    args = parser.parse_args()
    errors = validate_catalog()
    outputs = expected_outputs()
    if args.check:
        for path, expected in outputs.items():
            if not path.is_file():
                errors.append(f"Generated documentation is missing: {path.relative_to(ROOT)}")
            elif path.read_text(encoding="utf-8") != expected:
                errors.append(f"Generated documentation is stale: {path.relative_to(ROOT)}")
        if errors:
            print("\n".join(f"ERROR: {error}" for error in errors))
            return 1
        print(f"documentation catalog OK: {len(DOCUMENTS)} documents, {len(outputs)} generated files")
        return 0
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors))
        return 1
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
