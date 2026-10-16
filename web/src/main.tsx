import React from 'react'
import ReactDOM from 'react-dom/client'
import './style.css'
import './dashboard.css'
import { Dashboard } from './dashboard'

const repository = 'https://github.com/okiNa4431/ai-trading-bot'

function SymbolMark() {
  return <svg width="32" height="32" viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <circle cx="16" cy="16" r="12" stroke="currentColor" strokeWidth="1.3" />
    <ellipse cx="16" cy="16" rx="5" ry="12" stroke="currentColor" strokeWidth="1.3" />
    <path d="M4 16h24M7 8.5h18M7 23.5h18" stroke="currentColor" strokeWidth="1.1" />
  </svg>
}

function App() {
  return <div className="site">
    <a className="skip" href="#main">本文へ移動</a>
    <header className="flex items-center justify-between gap-4">
      <a className="brand flex items-center gap-3" href="#"><SymbolMark /><span>AI <span className="muted">vs</span> オルカン</span></a>
      <a className="repo-link" href={repository} target="_blank" rel="noreferrer">GitHub <span aria-hidden="true">↗</span></a>
    </header>

    <main id="main">
      <Dashboard />
      <section className="hero" aria-labelledby="title">
        <div className="hero-copy">
          <p className="eyebrow"><span className="status-dot" /> OPEN INVESTMENT EXPERIMENT</p>
          <h1 id="title">同じ100万円。<br />ふたつの<span className="accent">運用思想。</span></h1>
          <p className="intro">AIは、世界をまるごと持つ投資にどう挑むのか。<br className="desktop-break" />日本株・米国株の仮想売買と、オルカンへの一括投資。<br className="desktop-break" />同じ出発点から、その過程を公開する実験です。</p>
          <div className="hero-actions flex flex-wrap items-center gap-5">
            <a className="primary" href="#experiment">実験の設計を見る <span aria-hidden="true">↓</span></a>
            <span className="preparing"><span className="status-dot" /> 仮想運用プロジェクト</span>
          </div>
        </div>
        <aside className="experiment-card" aria-label="実験の現在地">
          <div className="flex items-center justify-between"><span className="eyebrow">EXPERIMENT 001</span><span className="phase-badge">PHASE 04</span></div>
          <p className="card-label">共通の初期仮想資金</p>
          <p className="capital"><span>¥</span>1,000,000</p>
          <div className="comparison">
            <div><span className="strategy-number">01 / ACTIVE</span><h2>AI</h2><p>日本株・米国株を選ぶ</p><span className="strategy-note">Gemini · AI判断モデル</span></div>
            <span className="versus">vs</span>
            <div><span className="strategy-number">02 / PASSIVE</span><h2>オルカン</h2><p>全世界株式を持つ</p><span className="strategy-note">一括投資 · 実基準価額で比較</span></div>
          </div>
          <p className="card-footnote">このWebは仮想運用の公開記録です。<br />実資金を使った運用ではありません。</p>
        </aside>
      </section>

      <section className="principles" id="experiment" aria-labelledby="design-title">
        <div className="section-heading"><p className="eyebrow">THE DESIGN</p><h2 id="design-title">判断より先に、<br className="mobile-break" />検証できる仕組みを。</h2><p>実資金を使わず、ルールと記録を積み上げる。</p></div>
        <div className="principle-grid">
          <article><span className="index">01</span><h3>日米、ふたつの市場</h3><p>日本株と米国株が対象。円とドルを分けて管理し、為替交換や取引コストも記録します。</p><span className="detail">JPY / USD</span></article>
          <article><span className="index">02</span><h3>仮想口座で確かめる</h3><p>現物株のみ、空売り・レバレッジなし。1銘柄10%、株式全体80%を買い注文の上限にします。</p><span className="detail">PAPER TRADING ONLY</span></article>
          <article><span className="index">03</span><h3>過程までオープンに</h3><p>データ取得から売買のルールまで、コードを公開。結果だけでなく、その前提も検証できる実験へ。</p><a className="detail text-link" href={repository} target="_blank" rel="noreferrer">VIEW REPOSITORY ↗</a></article>
        </div>
      </section>

      <section className="roadmap" aria-labelledby="roadmap-title">
        <div><p className="eyebrow">WORK IN PROGRESS</p><h2 id="roadmap-title">いまは、土台をつくる段階。</h2><p className="muted">実装済みの範囲と、これからを分けて公開します。</p></div>
        <ol>
          <li><span className="step">01</span><div><span className="step-state">実装・検証済み</span><h3>市場データ基盤</h3><p>日米株価の取得・検証・保存</p></div><span className="done" aria-label="完了">✓</span></li>
          <li><span className="step">02</span><div><span className="step-state">実装・検証済み</span><h3>仮想口座と売買エンジン</h3><p>残高・約定・損益・リスクの管理</p></div><span className="done" aria-label="完了">✓</span></li>
          <li><span className="step">03</span><div><span className="step-state">実装・検証済み</span><h3>AI判断エンジン</h3><p>定量スクリーニング・構造化判断・注文計画</p></div><span className="done">✓</span></li><li className="current"><span className="step">04</span><div><span className="step-state">現在の開発フェーズ</span><h3>運用の永続化と比較</h3><p>継続運用・オルカン比較・結果の公開</p></div></li>
        </ol>
      </section>
      <section className="closing"><SymbolMark /><p>結論を急がず、実験を重ねる。</p><a href={repository} target="_blank" rel="noreferrer">GitHubで開発を追う ↗</a></section>
    </main>
    <footer><span>AI vs オルカン <span className="muted">/ OPEN EXPERIMENT</span></span><p>仮想売買の開発実験です。投資助言・運用成績の保証は行いません。</p></footer>
  </div>
}

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
