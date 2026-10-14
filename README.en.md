<div align="center">

<img src="docs/assets/logo.svg" width="80" alt="BidRadar logo">

# BidRadar

**An evidence-first research agent for public tenders**

Helps software and AI service teams read public procurement notices: what a project requires, whether we can deliver it, and what is still unknown.<br>
Every finding cites the source text.

[![Tests](https://github.com/chasen2041maker/BidRadar/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/chasen2041maker/BidRadar/actions/workflows/tests.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)

[简体中文](README.md) · **English**

</div>

## Core ideas

1. **Evidence first.** Every finding must cite source text. Anything without support is marked "unknown", and unknown is never treated as met.
2. **Frozen inputs.** Each report is pinned to the notice version and company profile version it was built from. New material produces a new report; old reports never change.
3. **Budgeted model calls.** Cost is reserved before each call and settled afterwards. When a result is uncertain, the task stops for a human instead of retrying.
4. **Humans decide.** No win-rate predictions, no automatic bidding, no contacting buyers. Pursuing or passing on a project is a human decision.

## Plan

The legacy version already runs end to end: company profile → opportunity catalog → AI research → follow-up questions → change tracking. The current work is the **v2 rewrite**, which does three things:

- **Architecture:** merge four services into one modular monolith (FastAPI + PostgreSQL); split services later, once the boundaries are proven.
- **Research:** replace one large model call with a five-step pipeline (parse → extract → match → verify → compose), where every step can be tested and measured on its own.
- **Material:** uploaded tender documents become the main input; public collection is only for discovering projects.

| Stage | Scope | Status |
| --- | --- | --- |
| [0 Foundation](docs/roadmap.md#阶段-0-地基) | Project skeleton, database, migrations, CI | Done |
| [1 Identity & workspaces](docs/roadmap.md#阶段-1-身份与公司) | Login, three roles, company profiles, data isolation (RLS) | **Next** |
| [2 Catalog & ingestion](docs/roadmap.md#阶段-2-商机与采集) | Port the notice catalog and collectors | Planned |
| [3 Documents](docs/roadmap.md#阶段-3-文档与切片) | Upload and parse tender documents (the first background job) | Planned |
| [4 Research pipeline](docs/roadmap.md#阶段-4-研究流水线) | Five steps, cost ledger, evaluation | Planned |
| [5 Follow-up & tracking](docs/roadmap.md#阶段-5-追问与跟踪) | Questions, watches, change alerts, automatic re-checks | Planned |
| [6 Wrap-up](docs/roadmap.md#阶段-6-收尾) | Frontend on the new API, legacy code removed | Planned |

Each stage links to its guide in the roadmap (in Chinese): the tables and APIs to build, the order of work, which part the owner writes, and when the stage is done.

**Further out:** split the worker side first once boundaries are stable; Redis, Kubernetes and cloud deployment only when there is a clear need.

## Architecture

Every box is labelled with the stage that builds it.

```mermaid
flowchart TB
    web["Browser · frontend<br/>stage 6"]
    subgraph API["API process · FastAPI"]
        m1["identity · workspace<br/>login, companies, profiles<br/>stage 1"]
        m2["catalog<br/>notice catalog<br/>stage 2"]
        m3["documents<br/>tender files<br/>stage 3"]
        m4["research<br/>research, follow-ups<br/>stages 4–5"]
        m5["tracking<br/>decisions, alerts<br/>stage 5"]
    end
    subgraph Worker["Worker process"]
        w1["ingestion<br/>collect notices<br/>stage 2"]
        w2["document parsing<br/>stage 3"]
        w3["agent<br/>research pipeline<br/>stage 4"]
        w4["change detection<br/>automatic re-checks<br/>stage 5"]
    end
    db[("PostgreSQL 16 · stage 0<br/>one schema per module<br/>job and event tables")]
    ext["DeepSeek API<br/>public procurement sites"]
    web -- "cookie session" --> API
    API -- "business data<br/>+ jobs and events, one transaction" --> db
    Worker -- "claim jobs<br/>write results" --> db
    Worker -- "HTTPS" --> ext
    classDef done fill:#DCEFE6,stroke:#1A6B55,color:#12382C
    classDef next fill:#FBEBD0,stroke:#C77D1A,color:#5A3A0A
    class db done
    class m1 next
```

<sub>Green: done · Orange: next · Others: planned</sub>

## Screenshot

<img src="docs/assets/screenshot-catalog.png" alt="BidRadar opportunity catalog">

<sub>All companies and notices in the screenshot are fictional demo data.</sub>

## Quick start

Try the full flow (legacy, Python 3.10+ only):

```bash
python scripts/run_research_workbench.py    # open http://127.0.0.1:8965/
```

Test accounts are in `.bidradar-data/r12-workbench/demo-accounts.json`. In the catalog, switch the data scope to 虚构演示样本 (fictional demo sample) to see the demo notice.

Develop v2 (Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker):

```bash
docker compose up -d --wait db && uv run alembic upgrade head
uv run uvicorn bidradar.app.main:app        # open http://127.0.0.1:8000/health
```

## Learn more

The documents below are in Chinese.

- [Roadmap](docs/roadmap.md): what each stage builds, how to do it, and progress
- [v2 blueprint](docs/v2-blueprint.md): why the rewrite and what it becomes
- [Research agent](docs/agent.md): how research works, and test results
- [Dev guide](docs/dev-guide.md) · [Decision records](docs/decisions/) · [Contributing rules](AGENTS.md)
