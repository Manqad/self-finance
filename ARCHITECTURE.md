# System Architecture: Personal Treasury OS (Batwa-Parity + Month 1 Defense)

## 1. Directory Structure
```text
personal-treasury-os/
├── .env.example
├── .gitignore
├── requirements.txt
├── ARCHITECTURE.md
├── SPECS.md
├── TASKS.md
├── credentials.json          # Google Cloud OAuth Client Secret (User provided)
├── token.json                # Auto-generated OAuth token cache
├── data/
│   ├── treasury.db           # Local SQLite database
│   └── backups/              # One-click JSON/CSV database exports
├── src/
│   ├── __init__.py
│   ├── models.py             # Strict Pydantic data contracts
│   ├── database/
│   │   ├── __init__.py
│   │   ├── db_manager.py     # SQLite connection, migrations, CRUD, atomic balance updates
│   │   └── seed_rules.py     # Pre-seeded Pakistani merchants, banks, and category budgets
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── gmail_service.py  # Gmail API & IMAP fallback fetcher
│   │   ├── hmb_parser.py     # HabibMetro forwarded email regex/DOM parser
│   │   ├── nayapay_parser.py # NayaPay receipt email regex/DOM parser (inc. Raast/Card tags)
│   │   └── pdf_importer.py   # Multi-bank PDF & CSV statement parser (NayaPay & HabibMetro)
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── categorizer.py    # Deterministic keyword & Raast IBAN/Payee memory engine
│   │   ├── recurring.py      # Subscription & recurring bill detector
│   │   └── metrics.py        # Batwa analytics + Month 1 Financial Defense math engine
│   └── ui/
│       ├── __init__.py
│       ├── app.py            # Main Streamlit entrypoint
│       ├── components.py     # Wallet cards, burn gauges, charts, review queue banner
│       └── views.py          # 5 Views: Home/Wallets, Ledger/Review, Budgets, Analytics, Settings
└── tests/
    ├── __init__.py
    ├── fixtures/
    │   ├── sample_hmb.html
    │   └── sample_nayapay.html
    ├── test_parsers.py
    ├── test_engine.py
    └── test_db.py