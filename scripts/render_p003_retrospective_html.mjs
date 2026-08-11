#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { marked } = require("marked");

const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const source = path.join(root, "docs", "P003节点Wiki生产全流程复盘与Golden-Case工作流.md");
const target = path.join(root, "docs", "P003节点Wiki生产全流程复盘与Golden-Case工作流.html");
const markdown = fs.readFileSync(source, "utf8");

marked.setOptions({ gfm: true, breaks: false });
const article = marked.parse(markdown);

const html = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>P003 节点 Wiki 生产全流程复盘与 Golden Case 工作流</title>
  <style>
    :root {
      --paper: #f4f0e7;
      --paper-2: #ebe4d7;
      --ink: #182323;
      --muted: #61706c;
      --line: #c9c1b3;
      --signal: #e1512d;
      --signal-soft: #f6d6c9;
      --teal: #0b6f68;
      --teal-soft: #cfe3de;
      --navy: #102628;
      --code: #142d2e;
      --code-ink: #e4f0e9;
      --shadow: 0 20px 60px rgba(24, 35, 35, .12);
      --serif: "Iowan Old Style", "Songti SC", "STSong", "Noto Serif CJK SC", Georgia, serif;
      --sans: "Avenir Next", "PingFang SC", "Hiragino Sans GB", sans-serif;
      --mono: "IBM Plex Mono", "SFMono-Regular", Menlo, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; scroll-padding-top: 32px; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(rgba(16, 38, 40, .035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(16, 38, 40, .035) 1px, transparent 1px),
        var(--paper);
      background-size: 32px 32px;
      font: 16px/1.82 var(--sans);
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0 auto 0 0;
      width: 7px;
      background: var(--signal);
      z-index: 50;
    }
    #progress {
      position: fixed;
      inset: 0 0 auto 7px;
      height: 4px;
      transform-origin: left;
      transform: scaleX(0);
      background: var(--teal);
      z-index: 60;
    }
    .hero {
      position: relative;
      overflow: hidden;
      min-height: 540px;
      padding: 72px max(5vw, 32px) 58px;
      color: #f6f0e5;
      background: var(--navy);
      border-bottom: 1px solid rgba(255,255,255,.15);
    }
    .hero::after {
      content: "P003";
      position: absolute;
      right: -30px;
      bottom: -130px;
      color: rgba(255,255,255,.035);
      font: 900 clamp(13rem, 27vw, 27rem)/1 var(--sans);
      letter-spacing: -.09em;
      pointer-events: none;
    }
    .eyebrow {
      display: flex;
      gap: 12px;
      align-items: center;
      margin-bottom: 36px;
      color: #96c7bc;
      font: 700 12px/1 var(--mono);
      letter-spacing: .18em;
      text-transform: uppercase;
    }
    .eyebrow::before { content: ""; width: 42px; height: 2px; background: var(--signal); }
    .hero-grid { position: relative; z-index: 2; display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(310px, .55fr); gap: 8vw; align-items: end; }
    .hero h1 {
      max-width: 960px;
      margin: 0;
      font: 700 clamp(3rem, 6vw, 6.6rem)/.98 var(--serif);
      letter-spacing: -.055em;
    }
    .hero h1 span { display: block; color: #f0926f; }
    .hero p { max-width: 760px; margin: 34px 0 0; color: #bdcbc6; font-size: 18px; line-height: 1.8; }
    .hero-meta { border-top: 1px solid rgba(255,255,255,.22); }
    .meta-row { display: grid; grid-template-columns: 95px 1fr; gap: 18px; padding: 15px 0; border-bottom: 1px solid rgba(255,255,255,.15); }
    .meta-row b { color: #829c96; font: 600 11px/1.4 var(--mono); letter-spacing: .12em; text-transform: uppercase; }
    .meta-row span { color: #f6f0e5; font-size: 14px; }
    .metrics {
      position: relative;
      z-index: 3;
      display: grid;
      grid-template-columns: repeat(4, minmax(130px, 1fr));
      max-width: 1180px;
      margin: 54px 0 0;
      border: 1px solid rgba(255,255,255,.18);
      background: rgba(255,255,255,.035);
      backdrop-filter: blur(10px);
    }
    .metric { padding: 18px 22px; border-right: 1px solid rgba(255,255,255,.15); }
    .metric:last-child { border: 0; }
    .metric strong { display: block; color: #f6f0e5; font: 700 30px/1 var(--serif); }
    .metric small { display: block; margin-top: 8px; color: #91aaa4; font: 600 10px/1.4 var(--mono); letter-spacing: .09em; text-transform: uppercase; }
    .shell { display: grid; grid-template-columns: 310px minmax(0, 980px); gap: 64px; max-width: 1420px; margin: 0 auto; padding: 58px 44px 120px; align-items: start; }
    aside { position: sticky; top: 24px; max-height: calc(100vh - 48px); overflow: auto; padding: 22px 18px 24px 0; scrollbar-width: thin; }
    .tools { display: flex; gap: 8px; margin-bottom: 18px; }
    button, input { font: inherit; }
    .icon-btn {
      border: 1px solid var(--line);
      padding: 9px 11px;
      color: var(--ink);
      background: rgba(255,255,255,.28);
      cursor: pointer;
      font: 650 11px/1 var(--mono);
      letter-spacing: .06em;
    }
    .icon-btn:hover { color: white; border-color: var(--teal); background: var(--teal); }
    .search { position: relative; margin-bottom: 24px; }
    .search input { width: 100%; border: 0; border-bottom: 2px solid var(--ink); padding: 11px 2px; color: var(--ink); background: transparent; outline: 0; }
    .search input:focus { border-color: var(--signal); }
    .toc-title { margin: 0 0 14px; color: var(--signal); font: 700 11px/1 var(--mono); letter-spacing: .16em; text-transform: uppercase; }
    #toc { display: grid; gap: 2px; }
    #toc a { display: block; padding: 7px 10px; border-left: 2px solid transparent; color: var(--muted); text-decoration: none; font-size: 12px; line-height: 1.45; }
    #toc a.level-3 { padding-left: 22px; font-size: 11px; }
    #toc a:hover, #toc a.active { color: var(--ink); border-left-color: var(--signal); background: rgba(255,255,255,.34); }
    main { min-width: 0; }
    .notice {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 18px;
      margin: 0 0 38px;
      padding: 22px 24px;
      border: 1px solid #b8cfc9;
      border-left: 5px solid var(--teal);
      background: rgba(207,227,222,.55);
      box-shadow: var(--shadow);
    }
    .notice b { color: var(--teal); font: 800 12px/1.5 var(--mono); letter-spacing: .09em; }
    .notice p { margin: 0; }
    article {
      padding: clamp(34px, 6vw, 78px);
      border: 1px solid var(--line);
      background: rgba(250,247,240,.92);
      box-shadow: var(--shadow);
    }
    article > h1 { display: none; }
    article h2 {
      position: relative;
      margin: 92px 0 30px;
      padding: 18px 0 18px 64px;
      border-top: 2px solid var(--ink);
      border-bottom: 1px solid var(--line);
      font: 700 clamp(1.8rem, 3vw, 3rem)/1.2 var(--serif);
      letter-spacing: -.035em;
    }
    article h2::before {
      content: attr(data-index);
      position: absolute;
      left: 0;
      top: 23px;
      color: var(--signal);
      font: 800 14px/1 var(--mono);
      letter-spacing: .08em;
    }
    article h2:first-of-type { margin-top: 0; }
    article h3 { margin: 54px 0 20px; color: var(--teal); font: 700 1.45rem/1.35 var(--serif); }
    article h4 { margin: 36px 0 12px; font: 750 1rem/1.5 var(--sans); letter-spacing: .02em; }
    article p { margin: 0 0 18px; }
    article strong { color: #0c524e; font-weight: 750; }
    article a { color: var(--teal); text-decoration-thickness: 1px; text-underline-offset: 3px; }
    article a:hover { color: var(--signal); }
    article hr { height: 1px; margin: 52px 0; border: 0; background: var(--line); }
    article blockquote { margin: 32px 0; padding: 22px 26px; border: 0; border-left: 5px solid var(--signal); color: #563126; background: var(--signal-soft); font: 600 1.08rem/1.8 var(--serif); }
    article ul, article ol { margin: 0 0 22px; padding-left: 1.4em; }
    article li { margin: 7px 0; padding-left: .35em; }
    article li::marker { color: var(--signal); font-weight: 800; }
    code { padding: .14em .38em; border: 1px solid #d4cdc0; color: #87412b; background: #f2e5da; font: .86em/1.5 var(--mono); overflow-wrap: anywhere; }
    pre { position: relative; overflow: auto; margin: 28px 0; padding: 24px 26px; border-left: 5px solid #3d8d83; color: var(--code-ink); background: var(--code); box-shadow: 0 14px 35px rgba(16,38,40,.18); }
    pre code { padding: 0; border: 0; color: inherit; background: none; font-size: 12px; line-height: 1.75; }
    .table-wrap { width: 100%; overflow-x: auto; margin: 26px 0 34px; border: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; min-width: 720px; font-size: 13px; line-height: 1.6; background: #fbf8f1; }
    th { padding: 13px 14px; border-right: 1px solid #355053; color: white; background: var(--navy); text-align: left; font: 650 11px/1.35 var(--mono); letter-spacing: .045em; }
    td { padding: 12px 14px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); vertical-align: top; }
    tr:nth-child(even) td { background: #f1ece2; }
    tr:hover td { background: #e5eee9; }
    .search-hidden { display: none !important; }
    mark { padding: 0 .12em; color: inherit; background: #ffd277; }
    .footer { max-width: 1420px; margin: -72px auto 0; padding: 0 44px 60px 418px; color: var(--muted); font: 11px/1.7 var(--mono); }
    @media (max-width: 1050px) {
      .hero-grid { grid-template-columns: 1fr; }
      .hero-meta { max-width: 560px; }
      .shell { grid-template-columns: 1fr; padding-inline: 24px; }
      aside { position: static; max-height: none; padding: 0; }
      #toc { grid-template-columns: repeat(2, minmax(0,1fr)); }
      #toc a.level-3 { display: none; }
      .footer { margin: 0; padding: 0 24px 48px; }
    }
    @media (max-width: 680px) {
      .hero { min-height: 0; padding: 56px 24px 42px; }
      .hero h1 { font-size: 3rem; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .metric:nth-child(2) { border-right: 0; }
      .metric:nth-child(-n+2) { border-bottom: 1px solid rgba(255,255,255,.15); }
      #toc { grid-template-columns: 1fr; }
      article { padding: 28px 20px 48px; }
      article h2 { padding-left: 42px; font-size: 1.75rem; }
      article h2::before { top: 24px; }
      .notice { grid-template-columns: 1fr; }
    }
    @media print {
      :root { --paper: white; --ink: black; }
      body { background: white; font-size: 10.5pt; }
      body::before, #progress, aside, .tools, .footer { display: none; }
      .hero { min-height: 0; padding: 48px; color: black; background: white; border-bottom: 3px solid black; }
      .hero h1, .hero h1 span, .hero p, .meta-row span { color: black; }
      .hero::after, .metrics { display: none; }
      .hero-grid { grid-template-columns: 1fr; }
      .hero-meta { margin-top: 30px; }
      .meta-row { border-color: #aaa; }
      .shell { display: block; max-width: none; padding: 24px 0; }
      .notice { box-shadow: none; margin: 0 36px 24px; }
      article { padding: 0 36px; border: 0; box-shadow: none; background: white; }
      article h2 { break-after: avoid; margin-top: 38px; }
      article h3, table, pre, blockquote { break-inside: avoid; }
      table { font-size: 8pt; }
    }
  </style>
</head>
<body>
  <div id="progress"></div>
  <header class="hero">
    <div class="eyebrow">Autonomous Wiki Production · Incident Dossier 2026</div>
    <div class="hero-grid">
      <div>
        <h1>P003 节点 Wiki<span>生产全流程复盘</span></h1>
        <p>从二十余个批次、十一轮编辑修复和四代质量目标中，还原一个节点如何从“证据安全”走向“内容深入、可读、可发布”，并把经验固化为整个骨架数据库可复用的 Golden Case。</p>
      </div>
      <div class="hero-meta">
        <div class="meta-row"><b>NODE</b><span>ict_equipment::P003</span></div>
        <div class="meta-row"><b>IDENTITY</b><span>服务器 · 通用计算 · 刀片式</span></div>
        <div class="meta-row"><b>SCOPE</b><span>v3–v30 / production artifacts</span></div>
        <div class="meta-row"><b>RELEASE</b><span>Golden workflow design</span></div>
      </div>
    </div>
    <div class="metrics">
      <div class="metric"><strong>28</strong><small>traceable versions</small></div>
      <div class="metric"><strong>17</strong><small>workflow stages</small></div>
      <div class="metric"><strong>6</strong><small>execution layers</small></div>
      <div class="metric"><strong>100</strong><small>tests at v30</small></div>
    </div>
  </header>

  <div class="shell">
    <aside aria-label="文档目录">
      <div class="tools">
        <button class="icon-btn" type="button" onclick="window.print()">打印 / PDF</button>
        <button class="icon-btn" type="button" id="topBtn">回到顶部</button>
      </div>
      <label class="search">
        <input id="search" type="search" placeholder="过滤章节，例如：Agent / v28 / Table" aria-label="过滤文档章节">
      </label>
      <p class="toc-title">Document Index</p>
      <nav id="toc"></nav>
    </aside>

    <main>
      <div class="notice">
        <b>READING NOTE</b>
        <p>版本号不等于正文改写次数。早期回合主要建立 Agent 证明、证据核验和发布事务；v27 以后才依次解决内容深度、文章连贯、读者界面、定量表格和中国地域数据。</p>
      </div>
      <article id="report">${article}</article>
    </main>
  </div>
  <footer class="footer">Generated from the authoritative Markdown retrospective · Offline, self-contained HTML · lca-project / 2026-08-11</footer>

  <script>
    const report = document.getElementById('report');
    const toc = document.getElementById('toc');
    const slug = (s) => s.trim().toLowerCase().replace(/[\\s/]+/g, '-').replace(/[^\\w\\-\\u3400-\\u9fff]/g, '');
    const headings = [...report.querySelectorAll('h2, h3')];
    let sectionIndex = 0;
    headings.forEach((heading, index) => {
      if (heading.tagName === 'H2') {
        sectionIndex += 1;
        heading.dataset.index = String(sectionIndex).padStart(2, '0');
      }
      heading.id = slug(heading.textContent) || 'section-' + index;
      const link = document.createElement('a');
      link.href = '#' + heading.id;
      link.textContent = heading.textContent;
      link.className = heading.tagName === 'H3' ? 'level-3' : 'level-2';
      toc.appendChild(link);
    });
    report.querySelectorAll('table').forEach((table) => {
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    });

    const progress = document.getElementById('progress');
    const syncProgress = () => {
      const max = document.documentElement.scrollHeight - innerHeight;
      progress.style.transform = 'scaleX(' + (max > 0 ? scrollY / max : 0) + ')';
    };
    addEventListener('scroll', syncProgress, { passive: true });
    syncProgress();
    document.getElementById('topBtn').addEventListener('click', () => scrollTo({ top: 0, behavior: 'smooth' }));

    const links = [...toc.querySelectorAll('a')];
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a,b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (!visible) return;
      links.forEach((link) => link.classList.toggle('active', link.getAttribute('href') === '#' + visible.target.id));
    }, { rootMargin: '-10% 0px -75% 0px' });
    headings.forEach((heading) => observer.observe(heading));

    const blocks = [...report.querySelectorAll(':scope > h2')].map((heading, index, all) => {
      const nodes = [heading];
      let cursor = heading.nextElementSibling;
      while (cursor && cursor.tagName !== 'H2') { nodes.push(cursor); cursor = cursor.nextElementSibling; }
      return { heading, nodes, text: nodes.map((node) => node.textContent).join(' ').toLowerCase() };
    });
    document.getElementById('search').addEventListener('input', (event) => {
      const query = event.target.value.trim().toLowerCase();
      blocks.forEach((block) => {
        const show = !query || block.text.includes(query);
        block.nodes.forEach((node) => node.classList.toggle('search-hidden', !show));
      });
      links.forEach((link) => {
        const target = document.querySelector(link.getAttribute('href'));
        link.classList.toggle('search-hidden', !!query && target && target.closest('.search-hidden'));
      });
    });
  </script>
</body>
</html>`;

fs.writeFileSync(target, html, "utf8");
console.log(`${target} (${Buffer.byteLength(html, "utf8")} bytes)`);
