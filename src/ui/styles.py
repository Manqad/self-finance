"""Clean responsive dark-fintech stylesheet for Personal Treasury OS."""

from __future__ import annotations

import streamlit as st

APP_CSS = """
<style>
:root {
    --ptos-bg: #090E1A;
    --ptos-bg-card: rgba(17, 24, 39, 0.88);
    --ptos-bg-card-solid: #111827;
    --ptos-bg-soft: rgba(30, 41, 59, 0.62);
    --ptos-border: rgba(148, 163, 184, 0.16);
    --ptos-border-strong: rgba(148, 163, 184, 0.28);
    --ptos-text: #F8FAFC;
    --ptos-text-muted: #94A3B8;
    --ptos-green: #10B981;
    --ptos-blue: #3B82F6;
    --ptos-purple: #8B5CF6;
    --ptos-amber: #F59E0B;
    --ptos-red: #EF4444;
    --ptos-radius: 14px;
}

html, body, .stApp, div[data-testid="stAppViewContainer"], section[data-testid="stMain"] {
    background-color: var(--ptos-bg) !important;
    color: var(--ptos-text);
    max-width: 100vw !important;
    overflow-x: clip !important;
}

header[data-testid="stHeader"] {
    background: rgba(9, 14, 26, 0.72) !important;
    backdrop-filter: blur(8px);
    height: 2.45rem !important;
}

.block-container {
    padding-top: 2.85rem !important;
    padding-bottom: 2.75rem !important;
    padding-left: 1.25rem !important;
    padding-right: 1.25rem !important;
    max-width: 1240px !important;
    overflow-x: clip !important;
}

/* Custom sleek thin scrollbars */
::-webkit-scrollbar {
    width: 6px;
    height: 6px;
}
::-webkit-scrollbar-track {
    background: transparent;
}
::-webkit-scrollbar-thumb {
    background: rgba(148, 163, 184, 0.25);
    border-radius: 999px;
}
::-webkit-scrollbar-thumb:hover {
    background: rgba(148, 163, 184, 0.45);
}
* {
    scrollbar-width: thin;
    scrollbar-color: rgba(148, 163, 184, 0.25) transparent;
}

/* Contain data tables, editors, and charts strictly inside viewport */
div[data-testid="stDataFrame"],
div[data-testid="stDataEditor"],
div[data-testid="stPlotlyChart"],
div[data-testid="stPlotlyChart"] > div,
div.js-plotly-plot,
div.plot-container {
    max-width: 100% !important;
    width: 100% !important;
    box-sizing: border-box !important;
}
div[data-testid="stDataFrame"] > div,
div[data-testid="stDataEditor"] > div {
    max-width: 100% !important;
    overflow-x: auto !important;
}

/* Prevent iOS Safari zoom on form focus + keep tabular numerals */
input[type="text"],
input[type="password"],
input[type="number"],
input[type="email"],
textarea,
select,
div[data-baseweb="input"] input,
div[data-baseweb="select"] input {
    font-size: 16px !important;
}

/* Lock Screen */
.st-key-ptos_lock_shell {
    max-width: 430px;
    margin: 3rem auto 2rem auto;
    padding: 0 0.5rem;
}
.st-key-ptos_lock_shell div[data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border) !important;
    border-radius: 18px !important;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.48);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    padding: 0.55rem;
}
.st-key-ptos_lock_shell div[data-testid="stForm"] {
    border: none !important;
    padding: 0 !important;
    background: transparent !important;
}
.st-key-ptos_lock_shell div.stFormSubmitButton > button {
    min-height: 46px !important;
    font-size: 1rem !important;
    font-weight: 700 !important;
    border-radius: 12px !important;
}
.ptos-lock-hero {
    text-align: center;
    padding: 0.35rem 0 0.7rem 0;
}
.ptos-lock-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 52px;
    height: 52px;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(16, 185, 129, 0.2), rgba(59, 130, 246, 0.18));
    border: 1px solid rgba(16, 185, 129, 0.42);
    font-size: 1.5rem;
    margin-bottom: 0.5rem;
}
.ptos-lock-title {
    font-size: 1.35rem;
    font-weight: 800;
    color: var(--ptos-text);
    letter-spacing: -0.02em;
}
.ptos-lock-sub {
    font-size: 0.82rem;
    color: var(--ptos-text-muted);
    margin-top: 0.25rem;
}
.ptos-lock-badge {
    display: inline-block;
    margin-top: 0.45rem;
    padding: 0.18rem 0.62rem;
    border-radius: 999px;
    font-size: 0.74rem;
    font-weight: 700;
    color: #6EE7B7;
    background: rgba(16, 185, 129, 0.14);
    border: 1px solid rgba(16, 185, 129, 0.3);
}

/* Top Command Bar */
.ptos-brand-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.1rem 0;
}
.ptos-brand-title {
    font-size: 1.42rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: var(--ptos-text);
    display: flex;
    align-items: center;
    gap: 0.45rem;
    line-height: 1.2;
}
.ptos-brand-sub {
    font-size: 0.8rem;
    color: var(--ptos-text-muted);
    display: flex;
    align-items: center;
    gap: 0.45rem;
    flex-wrap: wrap;
}
.ptos-mode-pill {
    display: inline-flex;
    align-items: center;
    font-size: 0.68rem;
    font-weight: 700;
    padding: 0.12rem 0.48rem;
    border-radius: 999px;
    color: #A78BFA;
    background: rgba(139, 92, 246, 0.15);
    border: 1px solid rgba(139, 92, 246, 0.35);
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.ptos-sync-dot {
    display: inline-flex;
    align-items: center;
    font-size: 0.68rem;
    font-weight: 600;
    padding: 0.12rem 0.48rem;
    border-radius: 999px;
    color: #6EE7B7;
    background: rgba(16, 185, 129, 0.12);
    border: 1px solid rgba(16, 185, 129, 0.28);
}

/* Navigation Bar using native st.segmented_control */
.st-key-ptos_nav_container {
    margin: 0.45rem 0 0.9rem 0;
}
.st-key-ptos_nav_container div[data-testid="stButtonGroup"],
.st-key-ptos_nav_container div[data-testid="stBaseButtonGroup"] {
    width: 100% !important;
    background: rgba(15, 23, 42, 0.85) !important;
    border: 1px solid var(--ptos-border) !important;
    border-radius: var(--ptos-radius) !important;
    padding: 0.25rem !important;
    display: flex !important;
    flex-wrap: nowrap !important;
    gap: 0.25rem !important;
}
.st-key-ptos_nav_container button {
    flex: 1 1 0 !important;
    min-width: max-content !important;
    min-height: 40px !important;
    padding: 0.35rem 0.9rem !important;
    white-space: nowrap !important;
    border-radius: 10px !important;
    font-size: 0.86rem !important;
    font-weight: 600 !important;
    border: none !important;
    transition: background 0.15s ease, color 0.15s ease !important;
}
.st-key-ptos_nav_container button[aria-checked="true"],
.st-key-ptos_nav_container button[aria-pressed="true"] {
    background: linear-gradient(135deg, rgba(16, 185, 129, 0.24), rgba(59, 130, 246, 0.2)) !important;
    color: #FFFFFF !important;
    border: 1px solid rgba(16, 185, 129, 0.42) !important;
    font-weight: 700 !important;
    box-shadow: 0 2px 10px rgba(16, 185, 129, 0.16) !important;
}

/* Crisp filter pills inside Ledger */
.st-key-ptos_ledger_pills div[data-testid="stButtonGroup"],
.st-key-ptos_ledger_pills div[data-testid="stBaseButtonGroup"] {
    flex-wrap: wrap !important;
    gap: 0.35rem !important;
}
.st-key-ptos_ledger_pills button {
    white-space: nowrap !important;
    min-height: 36px !important;
    border-radius: 999px !important;
    font-size: 0.8rem !important;
}

/* Borders & cards */
div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: var(--ptos-radius) !important;
    border-color: var(--ptos-border) !important;
    background: rgba(15, 23, 42, 0.52);
}

/* Section Headers */
.ptos-section-head {
    margin: 0.95rem 0 0.5rem 0;
}
.ptos-section-title {
    font-size: 1.04rem;
    font-weight: 700;
    color: var(--ptos-text);
    letter-spacing: -0.01em;
}
.ptos-section-sub {
    font-size: 0.78rem;
    color: var(--ptos-text-muted);
    margin-top: 0.1rem;
}

/* Summary Strip (Liquid + Net Worth) */
.ptos-summary-strip {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.65rem;
    margin-bottom: 0.65rem;
}
.ptos-summary-pill {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 12px;
    padding: 0.62rem 0.92rem;
    backdrop-filter: blur(10px);
    gap: 0.45rem;
}
.ptos-summary-pill-accent {
    border-color: rgba(16, 185, 129, 0.35);
    background: linear-gradient(135deg, rgba(16, 185, 129, 0.12), rgba(15, 23, 42, 0.88));
}
.ptos-pill-label {
    font-size: 0.8rem;
    font-weight: 600;
    color: #CBD5E1;
}
.ptos-pill-value {
    font-size: 1.04rem;
    font-weight: 800;
    color: var(--ptos-text);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}

/* 4-Wallet Grid */
.ptos-wallet-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.65rem;
    margin-bottom: 0.9rem;
}
.ptos-wallet-card {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: var(--ptos-radius);
    padding: 0.82rem 0.9rem;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.24);
    transition: transform 0.15s ease, border-color 0.15s ease;
}
.ptos-wallet-card:hover {
    transform: translateY(-2px);
    border-color: var(--ptos-border-strong);
}
.ptos-wallet-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.45rem;
}
.ptos-wallet-icon {
    font-size: 1.2rem;
}
.ptos-wallet-badge {
    font-size: 0.66rem;
    font-weight: 700;
    padding: 0.12rem 0.44rem;
    border-radius: 999px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}
.ptos-wallet-name {
    font-size: 0.8rem;
    font-weight: 600;
    color: #CBD5E1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.ptos-wallet-bal {
    font-size: 1.18rem;
    font-weight: 800;
    margin-top: 0.2rem;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.ptos-wallet-sub {
    font-size: 0.7rem;
    color: var(--ptos-text-muted);
    margin-top: 0.15rem;
}

/* Burn Hero Card */
.ptos-hero-card {
    background: linear-gradient(145deg, rgba(17, 24, 39, 0.96), rgba(15, 23, 42, 0.84));
    border: 1px solid var(--ptos-border);
    border-radius: 16px;
    padding: 1.02rem 1.12rem;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.34);
    margin-bottom: 0.9rem;
}
.ptos-hero-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.65rem;
    flex-wrap: wrap;
    margin-bottom: 0.82rem;
}
.ptos-hero-eyebrow {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--ptos-green);
}
.ptos-hero-title {
    font-size: 1.12rem;
    font-weight: 700;
    color: var(--ptos-text);
}
.ptos-status-chip {
    font-size: 0.76rem;
    font-weight: 700;
    padding: 0.24rem 0.64rem;
    border-radius: 999px;
    border: 1px solid;
    white-space: nowrap;
}
.ptos-kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.6rem;
    margin-bottom: 0.85rem;
}
.ptos-kpi-box {
    background: rgba(15, 23, 42, 0.78);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 12px;
    padding: 0.62rem 0.74rem;
}
.ptos-kpi-label {
    font-size: 0.71rem;
    font-weight: 600;
    color: var(--ptos-text-muted);
}
.ptos-kpi-value {
    font-size: 1.12rem;
    font-weight: 800;
    color: var(--ptos-text);
    margin-top: 0.14rem;
    font-variant-numeric: tabular-nums;
}
.ptos-kpi-unit {
    font-size: 0.73rem;
    font-weight: 500;
    color: var(--ptos-text-muted);
}
.ptos-kpi-sub {
    font-size: 0.7rem;
    color: var(--ptos-text-muted);
    margin-top: 0.1rem;
}
.ptos-progress-wrap {
    margin-top: 0.55rem;
}
.ptos-emergency-wrap {
    margin-top: 0.78rem;
    padding-top: 0.68rem;
    border-top: 1px solid rgba(148, 163, 184, 0.12);
}
.ptos-progress-meta {
    display: flex;
    justify-content: space-between;
    font-size: 0.78rem;
    color: #CBD5E1;
    margin-bottom: 0.34rem;
    gap: 0.5rem;
    font-variant-numeric: tabular-nums;
}
.ptos-bar-track {
    width: 100%;
    height: 10px;
    background: rgba(15, 23, 42, 0.92);
    border-radius: 999px;
    overflow: hidden;
    border: 1px solid rgba(148, 163, 184, 0.12);
}
.ptos-bar-track-sm {
    height: 8px;
}
.ptos-bar-fill {
    height: 100%;
    border-radius: 999px;
    transition: width 0.25s ease;
}
.ptos-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.42rem;
    margin-top: 0.62rem;
}
.ptos-mini-chip {
    font-size: 0.73rem;
    font-weight: 600;
    color: #E2E8F0;
    background: rgba(30, 41, 59, 0.72);
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 999px;
    padding: 0.18rem 0.58rem;
    font-variant-numeric: tabular-nums;
}
.ptos-mini-chip-red {
    color: #FCA5A5;
    border-color: rgba(239, 68, 68, 0.28);
    background: rgba(239, 68, 68, 0.1);
}
.ptos-mini-chip-purple {
    color: #DDD6FE;
    border-color: rgba(139, 92, 246, 0.28);
    background: rgba(139, 92, 246, 0.12);
}

/* Needs Review Banner */
.ptos-review-banner {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;
    border-radius: 13px;
    padding: 0.68rem 0.92rem;
    margin-bottom: 0.6rem;
    border: 1px solid;
}
.ptos-review-alert {
    background: linear-gradient(135deg, rgba(245, 158, 11, 0.14), rgba(239, 68, 68, 0.08));
    border-color: rgba(245, 158, 11, 0.42);
}
.ptos-review-ok {
    background: rgba(16, 185, 129, 0.09);
    border-color: rgba(16, 185, 129, 0.3);
}
.ptos-review-left {
    display: flex;
    align-items: center;
    gap: 0.65rem;
}
.ptos-review-icon {
    font-size: 1.25rem;
}
.ptos-review-title {
    font-size: 0.9rem;
    font-weight: 700;
    color: var(--ptos-text);
}
.ptos-review-sub {
    font-size: 0.75rem;
    color: #CBD5E1;
}
.ptos-review-pill {
    font-size: 0.72rem;
    font-weight: 700;
    color: #FCD34D;
    background: rgba(245, 158, 11, 0.18);
    border: 1px solid rgba(245, 158, 11, 0.38);
    padding: 0.22rem 0.58rem;
    border-radius: 999px;
    white-space: nowrap;
}
.ptos-review-pill-ok {
    color: #6EE7B7;
    background: rgba(16, 185, 129, 0.16);
    border-color: rgba(16, 185, 129, 0.35);
}

/* Category Progress Bars */
.ptos-cat-list {
    display: flex;
    flex-direction: column;
    gap: 0.48rem;
}
.ptos-cat-card {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 12px;
    padding: 0.6rem 0.76rem;
}
.ptos-cat-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.34rem;
    gap: 0.5rem;
}
.ptos-cat-title {
    display: flex;
    align-items: center;
    gap: 0.38rem;
    flex-wrap: wrap;
}
.ptos-cat-emoji {
    font-size: 0.98rem;
}
.ptos-cat-name {
    font-size: 0.86rem;
    font-weight: 700;
    color: var(--ptos-text);
}
.ptos-bucket-pill {
    font-size: 0.64rem;
    font-weight: 700;
    color: var(--ptos-text-muted);
    background: rgba(30, 41, 59, 0.85);
    padding: 0.08rem 0.38rem;
    border-radius: 6px;
}
.ptos-over-badge {
    font-size: 0.66rem;
    font-weight: 700;
    color: #FCA5A5;
    background: rgba(239, 68, 68, 0.2);
    border: 1px solid rgba(239, 68, 68, 0.42);
    border-radius: 999px;
    padding: 0.08rem 0.42rem;
}
.ptos-near-badge {
    font-size: 0.66rem;
    font-weight: 700;
    color: #FCD34D;
    background: rgba(245, 158, 11, 0.18);
    border: 1px solid rgba(245, 158, 11, 0.4);
    border-radius: 999px;
    padding: 0.08rem 0.42rem;
}
.ptos-cat-pct {
    font-size: 0.84rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
}
.ptos-cat-row-bottom {
    display: flex;
    justify-content: space-between;
    font-size: 0.72rem;
    color: var(--ptos-text-muted);
    margin-top: 0.32rem;
    font-variant-numeric: tabular-nums;
}
.ptos-cat-spent {
    color: #CBD5E1;
    font-weight: 600;
    white-space: nowrap;
}

/* Upcoming Bills Checklist */
.ptos-bills-summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.79rem;
    font-weight: 600;
    color: #CBD5E1;
    margin-bottom: 0.48rem;
    padding: 0 0.1rem;
    gap: 0.4rem;
    flex-wrap: wrap;
}
.ptos-bills-due-total {
    color: #FCD34D;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
}
.ptos-bills-list {
    display: flex;
    flex-direction: column;
    gap: 0.44rem;
}
.ptos-bill-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.6rem;
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 12px;
    padding: 0.56rem 0.76rem;
}
.ptos-bill-title {
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--ptos-text);
}
.ptos-bill-meta {
    font-size: 0.71rem;
    color: var(--ptos-text-muted);
    margin-top: 0.08rem;
}
.ptos-bill-badge {
    font-size: 0.72rem;
    font-weight: 700;
    padding: 0.2rem 0.52rem;
    border-radius: 999px;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
}
.ptos-bill-paid {
    color: #6EE7B7;
    background: rgba(16, 185, 129, 0.14);
    border: 1px solid rgba(16, 185, 129, 0.34);
}
.ptos-bill-overdue {
    color: #FCA5A5;
    background: rgba(239, 68, 68, 0.16);
    border: 1px solid rgba(239, 68, 68, 0.38);
}
.ptos-bill-soon {
    color: #FCD34D;
    background: rgba(245, 158, 11, 0.16);
    border: 1px solid rgba(245, 158, 11, 0.38);
}
.ptos-bill-due {
    color: #CBD5E1;
    background: rgba(51, 65, 85, 0.5);
    border: 1px solid rgba(148, 163, 184, 0.28);
}

/* Mobile-First Transaction Cards */
.ptos-tx-feed {
    display: flex;
    flex-direction: column;
    gap: 0.44rem;
    margin-bottom: 0.7rem;
}
.ptos-tx-card {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 12px;
    padding: 0.62rem 0.8rem;
}
.ptos-tx-card-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.6rem;
}
.ptos-tx-title {
    font-size: 0.87rem;
    font-weight: 700;
    color: var(--ptos-text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.ptos-tx-amt {
    font-size: 0.91rem;
    font-weight: 800;
    white-space: nowrap;
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
}
.ptos-tx-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.32rem;
    margin-top: 0.36rem;
}
.ptos-tx-pill {
    font-size: 0.69rem;
    font-weight: 600;
    color: #CBD5E1;
    background: rgba(30, 41, 59, 0.78);
    border: 1px solid rgba(148, 163, 184, 0.15);
    border-radius: 999px;
    padding: 0.1rem 0.46rem;
}
.ptos-tx-pill-flow {
    color: #93C5FD;
    border-color: rgba(59, 130, 246, 0.25);
    background: rgba(59, 130, 246, 0.1);
}
.ptos-tx-pill-warn {
    color: #FCD34D;
    border-color: rgba(245, 158, 11, 0.35);
    background: rgba(245, 158, 11, 0.14);
}

/* Compact horizontal button rows that stay side-by-side on 390px screens */
div[class*="st-key-ptos_compact_"] div[data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    flex-direction: row !important;
    gap: 0.45rem !important;
}
div[class*="st-key-ptos_compact_"] div[data-testid="column"],
div[class*="st-key-ptos_compact_"] div[data-testid="stColumn"] {
    min-width: 0 !important;
}

@media (max-width: 900px) {
    .ptos-wallet-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .ptos-kpi-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

/* 2x2 crisp mobile KPI grid for 4-column metric rows */
@media (max-width: 640px) {
    div[class*="st-key-ptos_kpi_grid_"] div[data-testid="stHorizontalBlock"] {
        display: grid !important;
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
        gap: 0.45rem !important;
    }
    div[class*="st-key-ptos_kpi_grid_"] div[data-testid="stColumn"] {
        width: 100% !important;
        min-width: 0 !important;
    }
    div[class*="st-key-ptos_kpi_grid_"] div[data-testid="stMetric"] {
        background: var(--ptos-bg-card);
        border: 1px solid var(--ptos-border);
        border-radius: 12px;
        padding: 0.52rem 0.62rem;
    }
    div[class*="st-key-ptos_kpi_grid_"] div[data-testid="stMetricLabel"] {
        font-size: 0.72rem !important;
    }
    div[class*="st-key-ptos_kpi_grid_"] div[data-testid="stMetricValue"] {
        font-size: 1.08rem !important;
        line-height: 1.25 !important;
        font-variant-numeric: tabular-nums !important;
    }
    div[class*="st-key-ptos_kpi_grid_"] div[data-testid="stMetricDelta"] {
        font-size: 0.68rem !important;
    }
}

@media (max-width: 640px) {
    .block-container {
        padding-top: 2.75rem !important;
        padding-bottom: 2rem !important;
        padding-left: 0.62rem !important;
        padding-right: 0.62rem !important;
    }
    .ptos-brand-title {
        font-size: 1.15rem;
    }
    /* Crisp centered wrapping segmented nav pills on mobile with ZERO mid-word breaks */
    .st-key-ptos_nav_container div[data-testid="stButtonGroup"],
    .st-key-ptos_nav_container div[data-testid="stBaseButtonGroup"] {
        flex-wrap: wrap !important;
        justify-content: center !important;
        gap: 0.3rem !important;
        padding: 0.28rem !important;
    }
    .st-key-ptos_nav_container button {
        flex: 0 1 auto !important;
        min-width: max-content !important;
        min-height: 36px !important;
        padding: 0.28rem 0.68rem !important;
        font-size: 0.76rem !important;
    }
    .ptos-summary-strip {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.45rem;
    }
    .ptos-summary-pill {
        flex-direction: column;
        align-items: flex-start;
        padding: 0.5rem 0.66rem;
        gap: 0.14rem;
    }
    .ptos-pill-label {
        font-size: 0.7rem;
    }
    .ptos-pill-value {
        font-size: 0.94rem;
    }
    .ptos-wallet-grid {
        gap: 0.45rem;
    }
    .ptos-wallet-card {
        padding: 0.66rem 0.7rem;
    }
    .ptos-wallet-bal {
        font-size: 0.98rem;
    }
    .ptos-hero-card {
        padding: 0.75rem 0.74rem;
    }
    .ptos-kpi-value {
        font-size: 0.96rem;
    }
    .ptos-chip-row {
        display: grid !important;
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
        gap: 0.35rem !important;
    }
    .ptos-mini-chip {
        text-align: center !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    .ptos-review-banner {
        flex-wrap: wrap;
        padding: 0.6rem 0.72rem;
    }
    button[kind],
    div.stButton > button,
    div.stDownloadButton > button,
    div.stFormSubmitButton > button {
        min-height: 42px;
    }
}
</style>
"""


def inject_styles() -> None:
    """Inject the responsive dark-fintech CSS stylesheet once per run."""
    st.markdown(APP_CSS, unsafe_allow_html=True)
