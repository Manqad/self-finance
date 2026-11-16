### File 3: `AGENTS.md` (Multi-Agent Topology & File Ownership Locks)

```markdown
# AGENTS.md — 5-Agent Coordination Protocol

## Execution Topology
To prevent merge conflicts and race conditions, each agent owns specific files and directories. No agent may modify files outside its ownership boundary without Architect approval.

| Agent ID | Role | Owned Files / Directories | Upstream Dependency |
| :--- | :--- | :--- | :--- |
| **Agent 1** | Lead Architect & DevOps | `requirements.txt`, `pytest.ini`, `.gitignore`, `.env.example`, `src/__init__.py`, `src/models.py`, `data/default_rules.json` | None (Starts Phase 0) |
| **Agent 2** | Ingestion & Regex Parser Specialist | `src/ingestion/*` (`gmail_client.py`, `email_cleaner.py`, `parsers.py`) | `src/models.py` (Agent 1) |
| **Agent 3** | Database & Financial Engine Specialist | `src/db/*` (`schema.sql`, `repository.py`), `src/engine/*` (`categorizer.py`, `calculator.py`) | `src/models.py` (Agent 1) |
| **Agent 4** | Streamlit UI & Mobile UX Specialist | `src/ui/*` (`app.py`, `components.py`), `.streamlit/config.toml` | `src/db/repository.py`, `src/engine/calculator.py`, `src/ingestion/gmail_client.py` |
| **Agent 5** | QA, Fixtures & Browser Verification | `tests/*` (`fixtures/*`, `test_parsers.py`, `test_calculator.py`, `test_repository.py`) | All modules |

## Synchronized Phase Gates
1. **Gate 1 (Foundation)**: Agent 1 creates environment, installs packages, writes `src/models.py` and `data/default_rules.json`.
2. **Gate 2 (Parallel Core Build)**:
   - Agent 2 builds `src/ingestion/*`.
   - Agent 3 builds `src/db/*` and `src/engine/*`.
   - Agent 5 creates raw email test fixtures in `tests/fixtures/*`.
3. **Gate 3 (Unit Verification)**: Agent 5 runs `pytest tests/test_parsers.py tests/test_calculator.py tests/test_repository.py`. Any failures are routed back to Agent 2 or Agent 3 before UI wiring.
4. **Gate 4 (UI Integration)**: Agent 4 builds `src/ui/app.py` and `src/ui/components.py`, wiring the Sync button, manual cash entry, inline category override, and Month 1 gauges.
5. **Gate 5 (End-to-End Browser Verification)**: Agent 5 seeds the database with demo/fixture data, launches `streamlit run src/ui/app.py --server.port 8501 --server.headless true`, inspects the live DOM via Antigravity's headless browser, verifies zero runtime exceptions, and captures a screenshot artifact.