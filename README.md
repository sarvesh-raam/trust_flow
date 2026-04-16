# Trust Flow — Autonomous Customs Compliance Orchestrator

<p align="center">
  <img src="https://img.shields.io/badge/Status-Production--Ready-success?style=for-the-badge" alt="Status">
  <img src="https://img.shields.io/badge/Version-1.0.0-blue?style=for-the-badge" alt="Version">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/React-20232A?style=for-the-badge&logo=react&logoColor=61DAFB" alt="React">
  <img src="https://img.shields.io/badge/LangGraph-FFAB00?style=for-the-badge&logo=openai" alt="LangGraph">
  <img src="https://img.shields.io/badge/Firebase-FFCA28?style=for-the-badge&logo=firebase&logoColor=black" alt="Firebase">
</p>

> HackStrom'26 · Track 3 · Autonomous Document Processing & Compliance Agent  
> **Sarvesh Raam T K** × SRM Institute of Science and Technology

---

## 📖 Problem Statement
Manual customs document verification causes significant supply chain delays and results in costly fines for importers and exporters. Discrepancies between Commercial Invoices and Bills of Lading are common yet difficult to detect with traditional template-based systems. No existing solution handles complex layout analysis and multi-country jurisdictional rules while providing integrated human escalation in a single automated pipeline.

## Solution
Trust Flow utilizes a 12-node stateful LangGraph pipeline plus `END` to orchestrate document ingestion, structured data extraction, cross-document reconciliation, HS classification, and declaration generation. The system leverages Docling OCR with sub-pixel bounding box extraction to precisely anchor extracted data back to source PDFs. Human-in-the-Loop (HITL) escalation is natively integrated via LangGraph's `NodeInterrupt`, allowing operators to resolve blocking compliance issues through the declaration review interface or the new bill-chat editing flow. Operational transparency is maintained through a full audit trail and real-time observability powered by Prometheus, Grafana, and Loki.

---

## Architecture

### Design Patterns

| **Model** | Pydantic models & Firestore schemas | `backend/models.py`, `backend/repositories/run_repository.py` |
| **View** | React TypeScript SPA | `frontend/src/` |
| **Controller** | FastAPI route handlers | `backend/routes/upload.py`, `backend/routes/workflow.py`, `backend/routes/auth_routes.py` |

#### Consolidated Persistence — Firestore Repository
Based on `backend/repositories/run_repository.py`:
- **Single Source of Truth**: All workflow runs, audit trails, and declarations are persisted in Cloud Firestore.
- **Async Implementation**: Leverages `google-cloud-firestore-bundle` and `google-cloud-firestore` for high-concurrency async operations.
- **Failover Resilience**: Implements "Best Effort" persistence, ensuring the LangGraph pipeline continues even during transient cloud connectivity issues.

#### Event-Driven Architecture
| Component | Technology | Config |
|-----------|------------|--------|
| **Message broker** | Redis 7-alpine | Port 6379, persistent volume |
| **Task queue** | Celery 5.3.6 | `task_acks_late=True` for failure resilience |
| **Atomic pickup** | Celery Worker | `worker_prefetch_multiplier=1` (prevents task hoarding) |
| **Idempotency** | Celery Worker | `task_reject_on_worker_lost=True` (safe requeueing) |
| **Parallelisation** | Docker Compose | 2 Dedicated workers (`worker1`, `worker2`) |

---

## Agent Pipeline

### Topology
```
ingest ─► preprocess ─► ocr_extract ─┬─(conf < 0.7)─► vision_adjudication ─┐
                                      └─(conf ≥ 0.7)──────────────────────►─┤
                                                                             ▼
                                                                      field_extract
                                                                             │
                                                                        reconcile
                                                                             │
                                                                         hs_rag
                                                               (retrieve+rerank+generate)
                                                                             │
                                                            deterministic_validate
                                                            ┌────────────────┤
                                                       (BLOCK)          (no BLOCK)
                                                            ▼                ▼
                                                   interrupt_node    country_validate ◄─┘
                                                            └────────────────┘
                                                                             │
                                                                declaration_generate
                                                                             │
                                                                    audit_trace ─► END
```

### Pipeline Nodes (12 Operational Nodes + END)
| Node | Description |
|------|-------------|
| **ingest** | Receives document pair and assigns a unique `run_id` for tracking. |
| **preprocess** | Confirms file readability and prepares buffers for OCR/Vision. |
| **ocr_extract** | High-precision PDF layout analysis and text extraction using Docling. |
| **vision_adjudication** | Multi-modal fallback for low-confidence or heavily distorted document scans. |
| **field_extract** | LLM-driven structured extraction of key attributes (Invoice #, Dates, Weights). |
| **reconcile** | Cross-document validation (e.g., matching Gross Weight between Invoice and B/L). |
| **hs_rag** | Semantic retrieval of HS codes from a vector database for classification. |
| **deterministic_validate** | Rule-based checks for data formatting and required field presence. |
| **interrupt_node** | Pauses the graph using `NodeInterrupt` if blocking compliance issues are detected. |
| **country_validate** | Applies regional jurisdictional rules (KYC/AML/OFAC) via YAML configurations. |
| **declaration_generate** | Finalizes the Customs Declaration JSON and generates a human-readable summary. |
| **audit_trace** | Persists a detailed step-by-step history of agent reasoning to the database. |
| **END** | Final state terminal node; triggers post-processing hooks and user notification. |

### HITL Flow
1. **Detection**: `deterministic_validate` detects a `BLOCK-severity` issue (e.g., Weight Mismatch > 5%).
2. **Routing**: `_route_deterministic` detects the block and routes the state to `interrupt_node`.
3. **Suspension**: `interrupt_node` raises `NodeInterrupt`, triggering a state save and pausing the Celery task.
4. **Adjudication**: The operator views the conflict in the UI and submits a corrected value (e.g., manually verifying weight).
5. **Resume**: `POST /resume/{run_id}` updates the persisted state and resumes the graph from the point of interruption.

---

## REST API

| Method | Endpoint | Purpose | Auth |
|--------|----------|---------|------|
| **POST** | `/api/v1/auth/google` | Exchange Firebase ID token for JWT | None |
| **POST** | `/api/v1/upload/` | Ingest Invoice + B/L document pair | Bearer JWT |
| **POST** | `/api/v1/workflow/` | Trigger LangGraph pipeline | Bearer JWT |
| **GET** | `/api/v1/workflow/status/{run_id}` | Poll status + field-level bbox annotations | Bearer JWT |
| **POST** | `/api/v1/workflow/resume/{run_id}` | Submit HITL correction / manual override | Bearer JWT |
| **POST** | `/api/v1/workflow/chat/{run_id}` | Ask questions about the two bills and patch extracted fields | Bearer JWT |
| **GET** | `/api/v1/workflow/declaration/{run_id}` | Fetch final sanitized declaration JSON | Bearer JWT |
| **GET** | `/metrics` | Prometheus scrape endpoint | None |
| **GET** | `/health` | Application liveness check | None |

---

## Tech Stack

| Component | Technology | Version | Why Chosen |
|-----------|------------|---------|------------|
| **Agent Pipeline** | LangGraph | `0.1.0` | Stateful interruptible workflows with built-in checkpointing. |
| **OCR Engine** | Docling | `1.0.0` | State-of-the-art layout analysis with bbox provenance. |
| **LLM Client** | Instructor + OpenAI/Groq | `1.3.0` | Guarantees structured Pydantic extraction from unstructured text. |
| **Task Queue** | Celery + Redis | `5.3.6` | Support for atomic task pickup and idempotency on failure. |
| **Backend** | FastAPI + SQLModel | `0.111.0` | Asynchronous, high-performance, and auto-generated OpenAPI docs. |
| **Auth** | Firebase + PyJWT | `2.8.0` | Enterprise-grade Google SSO and stateless JWT sessions. |
| **Frontend** | React + React PDF | `18.x` | Interactive UI with live field-level bounding box overlays. |
| **Observability** | Prometheus + Grafana | `6.1.0` | Cloud-native monitoring of pipeline KPIs and agent latencies. |
| **Log Stack** | structlog + Loki | `24.1.0` | Structured JSON logging for deep tracing of agent reasoning. |

---

## Observability

| Service | Role | Port | Access |
|---------|------|------|--------|
| **Grafana** | KPI Dashboards, HITL Events, LLM Cost Tracking | 3001 | `localhost:3001` |
| **Prometheus** | Real-time metrics scraping and alerting | 9090 | `localhost:9090` |
| **Loki** | Aggregated log storage for all agent nodes | 3100 | via Grafana |
| **Flower** | Celery task monitoring and worker health | 5555 | `localhost:5555` |

**Error Tracing**:
- **structlog** emits JSON-structured events from the LangGraph nodes, including rich metadata for every failure.
- Every node execution automatically logs `node_name`, `latency_ms`, and summaries of both input and output state.
- These logs are streamed to **Loki** and queryable in Grafana, allowing for sub-second diagnosis of document extraction issues.

---

## Security

| Control | Implementation | Location |
|---------|---------------|----------|
| **PII in logs** | HMAC-SHA256 hashing — raw email never enters the trace | `backend/routes/auth_routes.py` |
| **PII in DB** | Firestore native encryption-at-rest for sensitive fields | GCP Default |
| **PII in API** | Strict Pydantic response models audit — no sensitive IDs | `backend/models.py` |
| **Authentication** | Firebase SSO + Google Identity Platform Integration | `backend/auth.py` |

---

## How to Run

### Prerequisites
- Docker 24+ and Docker Compose v2
- `ORGANIZER_API_KEY` (OpenAI-compatible) or `GROQ_API_KEY` (mandatory for LLM extraction)
- `FIREBASE_SERVICE_ACCOUNT_JSON` mapped string required in backend `.env` (Firebase is our primary DB)

### Quick Start
```bash
cp backend/.env.example backend/.env
# Place your base64 encoded string for FIREBASE_SERVICE_ACCOUNT_JSON in .env!
docker compose up -d --build
```

After startup:
- Open `http://localhost:3000`
- Sign in with Google if Firebase web auth is configured, or use `Continue Locally`
- Upload both the invoice and the bill of lading, then open the workflow run to review, chat, and edit

### Access
| Service | URL | Credentials |
|---------|-----|-------------|
| **Dashboard** | http://localhost:3000 | Google SSO or local guest |
| **API Docs** | http://localhost:8000/docs | N/A |
| **Grafana** | http://localhost:3001 | `admin` / `hackstrom2026` |
| **Flower** | http://localhost:5555 | N/A |

### Kubernetes (MiniKube Setup)
```bash
minikube start --memory=8192 --cpus=4
minikube addons enable ingress
eval $(minikube docker-env)
kubectl apply -f k8s/
```

---

## Screenshots
> *Dashboard Overview* — docs/screenshots/dashboard.png  
> *Agent Trace with Reasoning* — docs/screenshots/trace.png  
> *Document Adjudication UI* — docs/screenshots/hitl.png  
> *Compliance KPIs (Grafana)* — docs/screenshots/metrics.png

---

## 👥 Contributors
Developed with precision by the team for **HackStrom'26**:

- **Sarvesh Raam T K** ([github.com/sarvesh-raam](https://github.com/sarvesh-raam))
- **Mugundha S** ([github.com/abinavmugundhan](https://github.com/abinavmugundhan))
- **Mr G** ([github.com/Shiva085000](https://github.com/Shiva085000))

---
