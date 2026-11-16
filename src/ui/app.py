"""Streamlit entrypoint for Personal Treasury OS (Batwa-Parity + Month 1 Defense)."""

from __future__ import annotations

import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st
from dotenv import load_dotenv

from src.database.db_manager import DatabaseManager
from src.engine.metrics import compute_dashboard_metrics
from src.security import (
    get_auth_rate_limiter,
    get_cloud_sync_status,
    get_secret,
    get_session_timeout_seconds,
    is_auth_enabled,
    verify_password,
)
from src.ui.views import (
    ALL_TABS,
    TAB_BUDGETS,
    TAB_HOME,
    TAB_INSIGHTS,
    TAB_SETUP,
    TAB_TRANSACTIONS,
    render_budgets_tab,
    render_flash_banner,
    render_home_tab,
    render_insights_tab,
    render_sync_setup_tab,
    render_transactions_tab,
)

_MOBILE_CSS = """
<style>
:root {
    --ptos-bg-card: rgba(17, 24, 39, 0.88);
    --ptos-bg-elevated: rgba(30, 41, 59, 0.68);
    --ptos-border: rgba(148, 163, 184, 0.18);
    --ptos-text-muted: #94A3B8;
    --ptos-emerald: #10B981;
    --ptos-amber: #F59E0B;
    --ptos-crimson: #EF4444;
    --ptos-violet: #8B5CF6;
}

header[data-testid="stHeader"] {
    background: rgba(11, 17, 32, 0.88) !important;
    backdrop-filter: blur(12px) !important;
    -webkit-backdrop-filter: blur(12px) !important;
}

.stMainBlockContainer, .block-container {
    padding-top: 3.6rem !important;
    padding-bottom: 3.5rem;
    padding-left: 1.35rem;
    padding-right: 1.35rem;
    max-width: 1320px;
}

/* Custom sleek dark scrollbars */
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

/* Prevent iOS Safari automatic viewport zoom when focusing inputs */
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

/* Fintech Lock Screen */
.st-key-ptos_lock_shell {
    max-width: 440px;
    margin: 3rem auto 2rem auto;
    padding: 0 0.5rem;
}
.st-key-ptos_lock_shell div[data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border) !important;
    border-radius: 18px !important;
    box-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    padding: 0.5rem;
}
.st-key-ptos_lock_shell div[data-testid="stForm"] {
    border: none !important;
    padding: 0 !important;
    background: transparent !important;
}
.st-key-ptos_lock_shell div.stFormSubmitButton > button {
    min-height: 48px !important;
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
    font-size: 1.55rem;
    margin-bottom: 0.5rem;
}
.ptos-lock-title {
    font-size: 1.35rem;
    font-weight: 800;
    color: #F8FAFC;
    letter-spacing: -0.02em;
}
.ptos-lock-sub {
    font-size: 0.8rem;
    color: var(--ptos-text-muted);
    margin-top: 0.25rem;
}
.ptos-lock-badge {
    display: inline-block;
    margin-top: 0.45rem;
    padding: 0.16rem 0.6rem;
    border-radius: 999px;
    font-size: 0.74rem;
    font-weight: 700;
    color: #6EE7B7;
    background: rgba(16, 185, 129, 0.14);
    border: 1px solid rgba(16, 185, 129, 0.3);
}

/* Header & brand */
.ptos-brand-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    margin-bottom: 0.25rem;
}
.ptos-brand-title {
    font-size: 1.48rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: #F8FAFC;
    line-height: 1.2;
}
.ptos-brand-sub {
    font-size: 0.82rem;
    color: var(--ptos-text-muted);
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: center;
}
.ptos-mode-pill {
    display: inline-block;
    padding: 0.12rem 0.5rem;
    border-radius: 999px;
    font-size: 0.7rem;
    font-weight: 700;
    background: rgba(139, 92, 246, 0.16);
    color: #C4B5FD;
    border: 1px solid rgba(139, 92, 246, 0.35);
}

/* Glassmorphism surfaces for Streamlit native cards, metrics & expanders */
div[data-testid="stMetric"] {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 13px;
    padding: 0.65rem 0.8rem;
}
div[data-testid="stMetricLabel"] {
    font-size: 0.76rem !important;
    color: var(--ptos-text-muted) !important;
}
div[data-testid="stMetricValue"] {
    font-size: 1.25rem !important;
    font-weight: 800 !important;
    font-variant-numeric: tabular-nums;
}
div[data-testid="stExpander"] {
    background: rgba(15, 23, 42, 0.68);
    border: 1px solid var(--ptos-border) !important;
    border-radius: 14px !important;
}

/* Section headers */
.ptos-section-head {
    margin: 0.85rem 0 0.45rem 0;
}
.ptos-section-title {
    font-size: 1.02rem;
    font-weight: 700;
    color: #F8FAFC;
}
.ptos-section-sub {
    font-size: 0.78rem;
    color: var(--ptos-text-muted);
    margin-top: 0.08rem;
}

/* Summary Strip & Wallet Carousel */
.ptos-summary-strip {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.65rem;
    margin-bottom: 0.65rem;
}
.ptos-summary-pill {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 14px;
    padding: 0.72rem 0.95rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
}
.ptos-summary-pill-accent {
    border-color: rgba(16, 185, 129, 0.38);
    background: linear-gradient(135deg, rgba(16, 185, 129, 0.14), rgba(17, 24, 39, 0.92));
}
.ptos-pill-label {
    font-size: 0.78rem;
    color: var(--ptos-text-muted);
    font-weight: 600;
}
.ptos-pill-value {
    font-size: 1.05rem;
    font-weight: 800;
    color: #F8FAFC;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}

.ptos-wallet-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 0.7rem;
    margin-bottom: 0.85rem;
}
.ptos-wallet-card {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 15px;
    padding: 0.85rem 0.95rem;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.22);
    transition: transform 0.15s ease, border-color 0.15s ease;
}
.ptos-wallet-card:hover {
    transform: translateY(-2px);
    border-color: rgba(148, 163, 184, 0.36);
}
.ptos-wallet-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.35rem;
}
.ptos-wallet-icon {
    font-size: 1.28rem;
    line-height: 1;
}
.ptos-wallet-badge {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 0.16rem 0.48rem;
    border-radius: 999px;
}
.ptos-wallet-name {
    font-size: 0.86rem;
    font-weight: 600;
    color: #CBD5E1;
}
.ptos-wallet-bal {
    font-size: 1.28rem;
    font-weight: 800;
    margin-top: 0.16rem;
    font-variant-numeric: tabular-nums;
}
.ptos-wallet-sub {
    font-size: 0.74rem;
    color: var(--ptos-text-muted);
    margin-top: 0.2rem;
    font-variant-numeric: tabular-nums;
}

/* Burn Hero Card */
.ptos-hero-card {
    background: linear-gradient(145deg, rgba(17, 24, 39, 0.96), rgba(15, 23, 42, 0.92));
    border: 1px solid var(--ptos-border);
    border-radius: 16px;
    padding: 1.05rem 1.15rem;
    margin-bottom: 0.8rem;
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.28);
}
.ptos-hero-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 0.65rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
}
.ptos-hero-eyebrow {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--ptos-emerald);
}
.ptos-hero-title {
    font-size: 1.08rem;
    font-weight: 800;
    color: #F8FAFC;
    margin-top: 0.08rem;
}
.ptos-status-chip {
    font-size: 0.74rem;
    font-weight: 700;
    padding: 0.24rem 0.65rem;
    border-radius: 999px;
    border: 1px solid;
    white-space: nowrap;
}
.ptos-kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.65rem;
    margin-bottom: 0.8rem;
}
.ptos-kpi-box {
    background: rgba(15, 23, 42, 0.68);
    border: 1px solid rgba(148, 163, 184, 0.14);
    border-radius: 12px;
    padding: 0.7rem 0.8rem;
}
.ptos-kpi-label {
    font-size: 0.72rem;
    color: var(--ptos-text-muted);
    font-weight: 600;
}
.ptos-kpi-value {
    font-size: 1.2rem;
    font-weight: 800;
    color: #F8FAFC;
    margin-top: 0.16rem;
    font-variant-numeric: tabular-nums;
}
.ptos-kpi-unit {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--ptos-text-muted);
}
.ptos-kpi-sub {
    font-size: 0.71rem;
    color: var(--ptos-text-muted);
    margin-top: 0.16rem;
    font-variant-numeric: tabular-nums;
}

.ptos-progress-wrap {
    margin-top: 0.3rem;
}
.ptos-emergency-wrap {
    margin-top: 0.75rem;
    padding-top: 0.68rem;
    border-top: 1px solid rgba(148, 163, 184, 0.12);
}
.ptos-progress-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.45rem;
    font-size: 0.78rem;
    color: #CBD5E1;
    margin-bottom: 0.3rem;
    flex-wrap: wrap;
}
.ptos-bar-track {
    width: 100%;
    height: 10px;
    background: rgba(148, 163, 184, 0.15);
    border-radius: 999px;
    overflow: hidden;
}
.ptos-bar-track-sm {
    height: 7px;
}
.ptos-bar-fill {
    height: 100%;
    border-radius: 999px;
    transition: width 0.25s ease;
}
.ptos-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.55rem;
}
.ptos-mini-chip {
    font-size: 0.72rem;
    font-weight: 600;
    color: #CBD5E1;
    background: rgba(51, 65, 85, 0.45);
    border: 1px solid rgba(148, 163, 184, 0.16);
    padding: 0.18rem 0.5rem;
    border-radius: 8px;
    font-variant-numeric: tabular-nums;
}
.ptos-mini-chip-red {
    color: #FCA5A5;
    border-color: rgba(239, 68, 68, 0.28);
    background: rgba(239, 68, 68, 0.12);
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
    gap: 0.65rem;
    padding: 0.75rem 0.95rem;
    border-radius: 14px;
    border: 1px solid;
    margin-bottom: 0.75rem;
    flex-wrap: wrap;
}
.ptos-review-alert {
    background: rgba(245, 158, 11, 0.12);
    border-color: rgba(245, 158, 11, 0.42);
}
.ptos-review-ok {
    background: rgba(16, 185, 129, 0.1);
    border-color: rgba(16, 185, 129, 0.35);
}
.ptos-review-left {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    min-width: 0;
    flex: 1;
}
.ptos-review-icon {
    font-size: 1.25rem;
}
.ptos-review-title {
    font-size: 0.92rem;
    font-weight: 800;
    color: #F8FAFC;
}
.ptos-review-sub {
    font-size: 0.76rem;
    color: #CBD5E1;
}
.ptos-review-pill {
    font-size: 0.72rem;
    font-weight: 700;
    padding: 0.2rem 0.6rem;
    border-radius: 999px;
    background: rgba(245, 158, 11, 0.22);
    color: #FCD34D;
    white-space: nowrap;
}
.ptos-review-pill-ok {
    background: rgba(16, 185, 129, 0.2);
    color: #6EE7B7;
}

/* Category Budget Bars */
.ptos-cat-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}
.ptos-cat-card {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 13px;
    padding: 0.68rem 0.85rem;
}
.ptos-cat-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.45rem;
    margin-bottom: 0.38rem;
    flex-wrap: wrap;
}
.ptos-cat-title {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
}
.ptos-cat-emoji {
    font-size: 1rem;
}
.ptos-cat-name {
    font-size: 0.86rem;
    font-weight: 700;
    color: #F8FAFC;
}
.ptos-bucket-pill {
    font-size: 0.72rem;
    font-weight: 700;
    padding: 0.12rem 0.45rem;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.14);
    color: #CBD5E1;
}
.ptos-over-badge {
    font-size: 0.72rem;
    font-weight: 800;
    padding: 0.12rem 0.46rem;
    border-radius: 999px;
    background: rgba(239, 68, 68, 0.2);
    color: #FCA5A5;
}
.ptos-cat-row-bottom {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.45rem;
    margin-top: 0.28rem;
    font-variant-numeric: tabular-nums;
}
.ptos-cat-amounts {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    font-variant-numeric: tabular-nums;
}
.ptos-cat-spent {
    font-size: 0.78rem;
    font-weight: 600;
    color: #E2E8F0;
    white-space: nowrap;
}
.ptos-cat-pct {
    font-size: 0.82rem;
    font-weight: 800;
    white-space: nowrap;
}
.ptos-cat-rem {
    font-size: 0.74rem;
    color: var(--ptos-text-muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}

/* Upcoming Bills Checklist */
.ptos-bills-summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--ptos-bg-elevated);
    border: 1px solid var(--ptos-border);
    border-radius: 12px;
    padding: 0.55rem 0.8rem;
    font-size: 0.8rem;
    font-weight: 600;
    color: #E2E8F0;
    margin-bottom: 0.45rem;
    flex-wrap: wrap;
    gap: 0.35rem;
}
.ptos-bills-due-total {
    color: #FCD34D;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
}
.ptos-bills-list {
    display: flex;
    flex-direction: column;
    gap: 0.42rem;
    margin-bottom: 0.7rem;
}
.ptos-bill-row {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 12px;
    padding: 0.6rem 0.8rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}
.ptos-bill-title {
    font-size: 0.86rem;
    font-weight: 700;
    color: #F8FAFC;
}
.ptos-bill-meta {
    font-size: 0.73rem;
    color: var(--ptos-text-muted);
}
.ptos-bill-badge {
    font-size: 0.72rem;
    font-weight: 700;
    padding: 0.18rem 0.52rem;
    border-radius: 999px;
    white-space: nowrap;
}
.ptos-bill-paid {
    background: rgba(16, 185, 129, 0.16);
    color: #6EE7B7;
}
.ptos-bill-due {
    background: rgba(245, 158, 11, 0.16);
    color: #FCD34D;
}

/* Mobile-First Transaction Cards Feed */
.ptos-tx-feed {
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    margin-bottom: 0.75rem;
}
.ptos-tx-card {
    background: var(--ptos-bg-card);
    border: 1px solid var(--ptos-border);
    border-radius: 13px;
    padding: 0.68rem 0.85rem;
}
.ptos-tx-card-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 0.6rem;
}
.ptos-tx-title {
    font-size: 0.88rem;
    font-weight: 700;
    color: #F8FAFC;
    min-width: 0;
    flex: 1;
    line-height: 1.28;
    overflow-wrap: anywhere;
}
.ptos-tx-amt {
    font-size: 0.92rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.ptos-tx-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-top: 0.35rem;
    align-items: center;
}
.ptos-tx-pill {
    font-size: 0.74rem;
    font-weight: 600;
    color: #CBD5E1;
    background: rgba(51, 65, 85, 0.45);
    border: 1px solid rgba(148, 163, 184, 0.14);
    padding: 0.12rem 0.46rem;
    border-radius: 7px;
}
.ptos-tx-pill-flow {
    color: #93C5FD;
    background: rgba(59, 130, 246, 0.12);
    border-color: rgba(59, 130, 246, 0.25);
}
.ptos-tx-pill-warn {
    color: #FCD34D;
    background: rgba(245, 158, 11, 0.14);
    border-color: rgba(245, 158, 11, 0.32);
}
.ptos-tx-pill-ok {
    color: #6EE7B7;
    background: rgba(16, 185, 129, 0.12);
    border-color: rgba(16, 185, 129, 0.28);
}

/* Header Controls & Compact Multi-Column Preservation */
.st-key-ptos_hdr_controls div[data-testid="stHorizontalBlock"] {
    display: grid !important;
    grid-template-columns: 1.65fr 0.75fr !important;
    gap: 0.4rem !important;
    align-items: center !important;
}
.st-key-ptos_hdr_controls div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    width: 100% !important;
    min-width: 0 !important;
}

/* Touch-friendly Stateful Pill Navigation & Buttons */
.st-key-ptos_nav_container {
    margin-bottom: 0.45rem;
}
.st-key-ptos_nav_container div[data-testid="stRadio"] > div[role="radiogroup"] {
    display: flex;
    flex-direction: row;
    flex-wrap: nowrap;
    gap: 0.4rem;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    padding: 0.2rem 0.1rem 0.4rem 0.1rem;
    border-bottom: 1px solid rgba(148, 163, 184, 0.16);
}
.st-key-ptos_nav_container div[data-testid="stRadio"] > div[role="radiogroup"]::-webkit-scrollbar {
    display: none;
}
.st-key-ptos_nav_container label[data-baseweb="radio"] {
    background: rgba(15, 23, 42, 0.72);
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 10px;
    padding: 0.44rem 0.85rem;
    min-height: 44px;
    margin-right: 0 !important;
    white-space: nowrap;
    cursor: pointer;
    transition: all 0.15s ease;
    display: inline-flex !important;
    align-items: center !important;
}
.st-key-ptos_nav_container label[data-baseweb="radio"] p {
    color: var(--ptos-text-muted) !important;
    font-weight: 500 !important;
    white-space: nowrap !important;
    transition: color 0.15s ease;
}
.st-key-ptos_nav_container label[data-baseweb="radio"]:hover {
    border-color: rgba(16, 185, 129, 0.48);
    background: rgba(30, 41, 59, 0.85);
}
.st-key-ptos_nav_container label[data-baseweb="radio"]:hover p {
    color: #F8FAFC !important;
}
.st-key-ptos_nav_container label[data-baseweb="radio"] > div:first-child {
    display: none !important;
}
.st-key-ptos_nav_container label[data-baseweb="radio"]:has(input:checked) {
    background: linear-gradient(135deg, rgba(16, 185, 129, 0.26), rgba(59, 130, 246, 0.18)) !important;
    border-color: rgba(16, 185, 129, 0.72) !important;
    box-shadow: 0 2px 10px rgba(16, 185, 129, 0.18) !important;
}
.st-key-ptos_nav_container label[data-baseweb="radio"]:has(input:checked) p {
    color: #6EE7B7 !important;
    font-weight: 700 !important;
}
div.stButton > button, div.stDownloadButton > button, div.stFormSubmitButton > button {
    min-height: 42px;
    border-radius: 10px;
    font-weight: 600;
}

@media (max-width: 768px) {
    .stMainBlockContainer, .block-container {
        padding-top: calc(3.6rem + env(safe-area-inset-top, 0px)) !important;
        padding-bottom: calc(5.5rem + env(safe-area-inset-bottom, 0px)) !important;
        padding-left: 0.65rem !important;
        padding-right: 0.65rem !important;
    }
    .st-key-ptos_lock_shell {
        margin: 1.5rem auto 2rem auto;
    }
    .ptos-brand-title {
        font-size: 1.24rem;
    }
    .ptos-brand-sub {
        font-size: 0.76rem;
    }
    /* Tighten vertical block spacing & bordered card padding on mobile */
    div[data-testid="stVerticalBlock"] {
        gap: 0.62rem !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] > div {
        padding: 0.72rem 0.65rem !important;
    }
    /* Single-row horizontal scrollable pill rail (44px touch targets, clear scroll cue) */
    .st-key-ptos_nav_container div[data-testid="stRadio"] > div[role="radiogroup"] {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch !important;
        scrollbar-width: none !important;
        gap: 0.4rem !important;
        padding: 0.2rem 0.1rem 0.45rem 0.1rem !important;
    }
    .st-key-ptos_nav_container label[data-baseweb="radio"] {
        flex: 0 0 auto !important;
        min-height: 44px !important;
        padding: 0.42rem 0.82rem !important;
        justify-content: center !important;
    }
    .st-key-ptos_nav_container label[data-baseweb="radio"] p {
        font-size: 0.8rem !important;
        line-height: 1.2 !important;
    }
    /* Crisp button text wrapping & mobile radio sizing */
    div.stButton > button, div.stDownloadButton > button, div.stFormSubmitButton > button {
        white-space: normal !important;
        word-break: break-word !important;
        line-height: 1.25 !important;
        padding: 0.42rem 0.58rem !important;
    }
    div.stButton > button p, div.stDownloadButton > button p, div.stFormSubmitButton > button p {
        white-space: normal !important;
        word-break: break-word !important;
        line-height: 1.25 !important;
        font-size: 0.84rem !important;
    }
    div[data-testid="stRadio"]:not(.st-key-ptos_nav_container div[data-testid="stRadio"]) label[data-baseweb="radio"] {
        align-items: flex-start !important;
        padding: 0.35rem 0.2rem !important;
    }
    div[data-testid="stRadio"]:not(.st-key-ptos_nav_container div[data-testid="stRadio"]) label[data-baseweb="radio"] p {
        font-size: 0.86rem !important;
        line-height: 1.3 !important;
    }
    /* Keep compact 2-up metric grids & compact form rows on mobile */
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) {
        display: grid !important;
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
        gap: 0.45rem !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="stColumn"] {
        width: 100% !important;
        min-width: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="stColumn"]:last-child:nth-child(odd) {
        grid-column: 1 / -1 !important;
    }
    div[data-testid="stMetric"] {
        padding: 0.55rem 0.65rem !important;
    }
    div[data-testid="stMetricLabel"] {
        font-size: 0.72rem !important;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.04rem !important;
        overflow-wrap: anywhere;
    }
    div[class*="st-key-ptos_compact_"] div[data-testid="stHorizontalBlock"] {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: wrap !important;
        gap: 0.45rem !important;
    }
    div[class*="st-key-ptos_compact_"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
        flex: 1 1 calc(50% - 0.45rem) !important;
        min-width: 135px !important;
        width: auto !important;
    }
    /* 3-action compact category rows stay 3-up on 1 row on mobile */
    div[class*="st-key-ptos_compact_cat_"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
        flex: 1 1 calc(33.33% - 0.35rem) !important;
        min-width: 85px !important;
    }
    /* Tab 5 Backup/Cloud 3-button cards: full-width primary row + 2-up secondary buttons */
    div.st-key-ptos_compact_t5_bak div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:first-child,
    div.st-key-ptos_compact_t5_cloud div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:first-child {
        flex: 1 1 100% !important;
        width: 100% !important;
    }
    div.st-key-ptos_compact_t5_bak div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:not(:first-child),
    div.st-key-ptos_compact_t5_cloud div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:not(:first-child) {
        flex: 1 1 calc(50% - 0.35rem) !important;
        min-width: 130px !important;
    }
    /* Top Summary Strip stays 2-up on phone */
    .ptos-summary-strip {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.45rem;
    }
    .ptos-summary-pill {
        flex-direction: column;
        align-items: flex-start;
        padding: 0.62rem 0.72rem;
        gap: 0.14rem;
    }
    .ptos-pill-label {
        font-size: 0.74rem;
    }
    .ptos-pill-value {
        font-size: 1rem !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
        letter-spacing: -0.02em !important;
        max-width: 100%;
    }
    .ptos-wallet-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.45rem;
    }
    .ptos-wallet-card {
        padding: 0.7rem 0.72rem;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
    }
    .ptos-wallet-bal {
        font-size: 1.02rem !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    .ptos-kpi-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.45rem;
    }
    .ptos-kpi-box {
        padding: 0.6rem 0.65rem;
    }
    .ptos-kpi-value {
        font-size: 0.98rem;
        overflow-wrap: anywhere;
    }
    .ptos-hero-card {
        padding: 0.75rem 0.72rem;
        border-radius: 14px;
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
    .ptos-cat-spent,
    .ptos-tx-amt {
        white-space: nowrap !important;
        flex-shrink: 0 !important;
        font-variant-numeric: tabular-nums !important;
    }
}
</style>
"""


@st.cache_resource
def _get_db_manager(db_path: str) -> DatabaseManager:
    """Return a cached thread-safe SQLite DatabaseManager for db_path."""
    import src.database.db_manager as db_mod

    return db_mod.DatabaseManager(db_path=db_path)


def _resolve_default_month(
    db: DatabaseManager, available_months: list[str]
) -> str:
    """Default to current month if it has data, else latest month with transactions."""
    current_month = date.today().strftime("%Y-%m")
    if db.get_transactions(month_key=current_month):
        return current_month
    for month_key in available_months:
        if db.get_transactions(month_key=month_key):
            return month_key
    return available_months[0] if available_months else current_month


def _enforce_authentication_gate() -> bool:
    """Enforce zero-trust lock screen with PBKDF2 verification, lockout, and idle timeout."""
    if not is_auth_enabled():
        return True

    timeout_sec = get_session_timeout_seconds()
    if st.session_state.get("ptos_auth_ok"):
        if timeout_sec > 0:
            last_active = float(st.session_state.get("ptos_auth_last_active", 0.0))
            if (time.monotonic() - last_active) > timeout_sec:
                st.session_state["ptos_auth_ok"] = False
                st.session_state["ptos_auth_notice"] = (
                    "Session locked due to inactivity. Please enter your password/PIN to continue."
                )
            else:
                st.session_state["ptos_auth_last_active"] = time.monotonic()
                return True
        else:
            return True

    notice = st.session_state.get("ptos_auth_notice")
    has_cred = bool(
        get_secret("TREASURY_APP_PASSWORD_HASH") or get_secret("TREASURY_APP_PASSWORD")
    )

    with st.container(key="ptos_lock_shell"):
        with st.container(border=True):
            st.markdown(
                '<div class="ptos-lock-hero">'
                '<div class="ptos-lock-icon">🔐</div>'
                '<div class="ptos-lock-title">Personal Treasury OS</div>'
                '<div class="ptos-lock-sub">Authenticate to access your encrypted multi-wallet ledger.</div>'
                '<span class="ptos-lock-badge">Zero-Trust Vault • PBKDF2-SHA256</span>'
                "</div>",
                unsafe_allow_html=True,
            )
            if notice:
                st.info(notice)

            if not has_cred:
                st.error(
                    "Authentication is required (`TREASURY_REQUIRE_AUTH=1`), "
                    "but neither `TREASURY_APP_PASSWORD_HASH` nor `TREASURY_APP_PASSWORD` is configured."
                )
                return False

            limiter = get_auth_rate_limiter()
            locked, remaining_sec = limiter.is_locked()
            if locked:
                st.error(
                    f"Too many failed unlock attempts. Please wait {remaining_sec} second(s) before retrying."
                )

            with st.form(key="ptos_lock_form", clear_on_submit=True):
                candidate = st.text_input(
                    "Vault Password / PIN",
                    type="password",
                    key="ptos_lock_password",
                    disabled=locked,
                )
                submitted = st.form_submit_button(
                    "🔓 Unlock Vault",
                    type="primary",
                    width="stretch",
                    disabled=locked,
                )

            if submitted and not locked:
                if verify_password(candidate):
                    limiter.record_success()
                    st.session_state["ptos_auth_ok"] = True
                    st.session_state["ptos_auth_last_active"] = time.monotonic()
                    st.session_state.pop("ptos_auth_notice", None)
                    st.rerun()
                else:
                    cooldown = limiter.record_failure()
                    if cooldown > 0:
                        st.rerun()
                    else:
                        left = max(0, 5 - limiter.failure_count())
                        st.error(
                            f"Invalid password or PIN ({left} attempt(s) left before cooldown)."
                        )

    return False


def render_app(db: Optional[DatabaseManager] = None) -> None:
    """Render the complete Personal Treasury OS Streamlit application."""
    st.markdown(_MOBILE_CSS, unsafe_allow_html=True)

    if db is None and "PYTEST_CURRENT_TEST" not in os.environ:
        load_dotenv(override=False)

    if not _enforce_authentication_gate():
        return

    if db is None:
        db_path = os.getenv("TREASURY_DB_PATH", "data/treasury.db")
        db = _get_db_manager(db_path)
        import inspect

        needs_reload = any(
            not hasattr(db, attr)
            for attr in (
                "update_category_budget_by_id",
                "delete_category_budget",
                "find_internal_transfer_pairs",
                "link_internal_transfer_pair",
                "reconcile_existing_duplicates_and_payees",
                "sync_to_cloud_vault",
            )
        ) or (
            "target_account" not in inspect.signature(db.learn_and_apply_rule).parameters
        )
        if needs_reload:
            import importlib
            import src.database.db_manager as db_mod
            import src.database.seed_rules as seed_mod
            import src.engine.categorizer as cat_mod
            import src.ingestion.hmb_parser as hmb_mod
            import src.ingestion.nayapay_parser as np_mod
            import src.ingestion.pdf_importer as pdf_mod
            import src.security as sec_mod

            try:
                db.close()
            except Exception:
                pass
            importlib.reload(sec_mod)
            importlib.reload(seed_mod)
            importlib.reload(cat_mod)
            importlib.reload(db_mod)
            importlib.reload(hmb_mod)
            importlib.reload(np_mod)
            importlib.reload(pdf_mod)
            if hasattr(_get_db_manager, "clear"):
                _get_db_manager.clear()
            db = _get_db_manager(db_path)

    if (
        get_cloud_sync_status().get("configured")
        and get_secret("TREASURY_AUTO_RESTORE", "1").lower()
        not in {"0", "false", "no", "off"}
        and not st.session_state.get("ptos_cloud_boot_checked")
    ):
        st.session_state["ptos_cloud_boot_checked"] = True
        try:
            if len(db.get_transactions()) == 0:
                ok_boot, msg_boot = db.restore_from_cloud_vault()
                st.session_state["ptos_synced_changes"] = getattr(db, "change_counter", 0)
                st.session_state["ptos_last_cloud_sync"] = (ok_boot, msg_boot)
        except Exception:
            pass

    st.session_state.setdefault("ptos_synced_changes", getattr(db, "change_counter", 0))

    available_months = db.get_available_months()
    if (
        "ptos_active_month" not in st.session_state
        or st.session_state["ptos_active_month"] not in available_months
    ):
        st.session_state["ptos_active_month"] = _resolve_default_month(
            db, available_months
        )

    config = db.get_budget_config()

    # Top Header Bar: Brand + Compact Month Selector (+ Lock button when auth enabled)
    auth_active = is_auth_enabled()
    hdr_left, hdr_right = st.columns([2.0, 1.15], vertical_alignment="center")
    with hdr_left:
        env_badge = (
            '<span class="ptos-mode-pill">Envelope Mode ON</span>'
            if config.envelope_mode
            else ""
        )
        st.markdown(
            f'<div class="ptos-brand-wrap">'
            f'<div class="ptos-brand-title">🛡️ Personal Treasury OS</div>'
            f'<div class="ptos-brand-sub">'
            f"<span>Pakistani Multi-Wallet Treasury • Month 1 Defense</span>"
            f"{env_badge}"
            f"</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    with hdr_right:
        if auth_active:
            with st.container(key="ptos_hdr_controls"):
                hdr_m, hdr_lock = st.columns([1.65, 0.85], vertical_alignment="center")
                with hdr_m:
                    active_month = st.selectbox(
                        "Active Month",
                        options=available_months,
                        key="ptos_active_month",
                        label_visibility="collapsed",
                    )
                with hdr_lock:
                    if st.button("🔒 Lock", key="ptos_lock_btn", width="stretch"):
                        st.session_state["ptos_auth_ok"] = False
                        st.session_state["ptos_auth_notice"] = "Vault locked manually."
                        st.rerun()
        else:
            active_month = st.selectbox(
                "Active Month",
                options=available_months,
                key="ptos_active_month",
                label_visibility="collapsed",
            )

    _tab_to_slug = {
        TAB_HOME: "home",
        TAB_TRANSACTIONS: "transactions",
        TAB_BUDGETS: "budgets",
        TAB_INSIGHTS: "insights",
        TAB_SETUP: "setup",
    }
    _slug_to_tab = {slug: tab for tab, slug in _tab_to_slug.items()}

    pending_tab = st.session_state.pop("ptos_pending_tab", None)
    if pending_tab in ALL_TABS:
        st.session_state["ptos_active_tab"] = pending_tab
    elif "ptos_active_tab" not in st.session_state or st.session_state["ptos_active_tab"] not in ALL_TABS:
        qp_tab = st.query_params.get("tab", "")
        if qp_tab in ALL_TABS:
            st.session_state["ptos_active_tab"] = qp_tab
        elif qp_tab in _slug_to_tab:
            st.session_state["ptos_active_tab"] = _slug_to_tab[qp_tab]
        else:
            st.session_state["ptos_active_tab"] = TAB_HOME

    with st.container(key="ptos_nav_container"):
        active_tab = st.radio(
            "Navigation",
            options=list(ALL_TABS),
            horizontal=True,
            key="ptos_active_tab",
            label_visibility="collapsed",
        )
    st.query_params["tab"] = _tab_to_slug.get(active_tab, "home")

    flash_slot = st.container()
    with flash_slot:
        render_flash_banner()

    metrics = compute_dashboard_metrics(db, active_month)

    if active_tab == TAB_HOME:
        render_home_tab(db, metrics, active_month)
    elif active_tab == TAB_TRANSACTIONS:
        render_transactions_tab(db, active_month)
    elif active_tab == TAB_BUDGETS:
        render_budgets_tab(db, metrics, active_month)
    elif active_tab == TAB_INSIGHTS:
        render_insights_tab(db, metrics, active_month)
    elif active_tab == TAB_SETUP:
        render_sync_setup_tab(db, active_month)

    auto_push = get_secret("TREASURY_CLOUD_AUTO_PUSH", "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    current_changes = getattr(db, "change_counter", 0)
    if auto_push and get_cloud_sync_status().get("configured"):
        synced_changes = int(st.session_state.get("ptos_synced_changes", current_changes))
        if current_changes > synced_changes:
            if len(db.get_transactions()) > 0:
                sync_ok, sync_msg = db.sync_to_cloud_vault()
                st.session_state["ptos_synced_changes"] = getattr(
                    db, "change_counter", current_changes
                )
                st.session_state["ptos_last_cloud_sync"] = (sync_ok, sync_msg)
            else:
                st.session_state["ptos_synced_changes"] = current_changes
    else:
        st.session_state["ptos_synced_changes"] = current_changes


def main() -> None:
    """Streamlit entrypoint configuring page settings and rendering the app."""
    st.set_page_config(
        page_title="Personal Treasury OS",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    render_app()


if __name__ == "__main__":
    main()
