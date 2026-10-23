# Jigsaw Policy Assistant

Internal tool for Jigsaw Insurance staff. Underwriters, claims handlers and customer service colleagues ask
questions about our policy documents ("What is the escape of water excess?", "Is a courtesy car provided
after a write-off?") and get a short answer in which every statement links to the page of the policy wording,
endorsement or internal guide it came from.

- Answers come **only** from the documents your role can see, and from the right **version and region** of the
  wording. If a question gives an incident date, the wording in force on that date is used.
- When the product, region or version is unclear, the assistant **asks** instead of guessing.
- Every statement is **checked** against its cited text before it is shown; when nothing can be verified, the
  assistant says it cannot answer.
- It never makes coverage or claim decisions. Answers are reference information for staff.

## Running it

Everything runs in Docker. You need Docker Desktop (or Docker Engine with Compose v2) and an OpenAI API key.

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

In `.env`, set `POSTGRES_SUPERUSER_PASSWORD`, `APP_DB_PASSWORD`, `OPENAI_API_KEY` and, for the sample staff
accounts, `SEED_PASSWORD`. Then:

```bash
docker compose up -d --build                       # database, migrations, API and ingestion worker
docker compose run --rm api python -m app.cli seed # sample products, staff accounts and policy documents
```

Open http://localhost:8000 and sign in as one of the sample staff (password = `SEED_PASSWORD`):

| Account | Role | Sees |
|---|---|---|
| priya.shah@jigsaw-insurance.com | Administrator | everything, plus document upload, users, usage and audit |
| tom.whitfield@jigsaw-insurance.com | Underwriter | policy wordings, endorsements and the Home Underwriting Guide |
| aisha.okafor@jigsaw-insurance.com | Claims handler | policy wordings, endorsements and the Motor Claims Handling Manual |
| daniel.murphy@jigsaw-insurance.com | Customer service | policy wordings and endorsements |

The seed creates 53 sample staff in all (4 administrators, 17 underwriters, 16 claims handlers and 16 customer
service colleagues), every one at `firstname.lastname@jigsaw-insurance.com` with the same password; the full
list is `STAFF_BY_ROLE` in `app/cli.py` and the **Admin** page.

The worker indexes the documents in the background (usually under a minute); progress is on the **Documents** page.
API documentation is at http://localhost:8000/docs.

Useful commands:

```bash
docker compose logs -f api worker
docker compose run --rm api python -m app.cli retry-failed   # requeue failed documents (e.g. after fixing the API key)
echo 'a-strong-password' | docker compose run --rm -T api python -m app.cli create-user jo.bloggs@jigsaw-insurance.com "Jo Bloggs" underwriter
docker compose down            # stop (data is kept)
docker compose down --volumes  # stop and delete the database and uploaded files
```

## Sample data

`sample_data/` contains the Jigsaw product catalogue used for development and evaluation. The documents are
written in Markdown (`sample_data/documents/*.md`) and rendered to PDF with `sample_data/build_pdfs.py`:

| Product | Documents |
|---|---|
| Jigsaw Home Insurance | wording 2025.1 (UK, 1 Jan 2025 - 31 Mar 2026), wording 2026.1 (UK and Republic of Ireland), endorsement HE12 Escape of Water Amendment (from 1 Jul 2026), Home Underwriting Guide (underwriters only) |
| Jigsaw Car Insurance | wording 2026.1, endorsement M07 Telematics Condition (a **scanned** PDF, read with OCR), Motor Claims Handling Manual (claims handlers only) |
| Jigsaw Travel Insurance | wording 2026.1 |
| Jigsaw Shop and Office Insurance | wording 2026.1 (small business package) |

The versions differ on purpose (excesses, limits, unoccupancy periods, fees), so answering from the wrong
version is caught by the tests and the evaluation. After editing a source document, rebuild the PDFs with
`docker compose run --rm tests python sample_data/build_pdfs.py`.

## How a question is answered

Diagrams of the system, document ingestion and question answering are in [ARCHITECTURE.md](ARCHITECTURE.md).

```
question + selection (product, region, version, incident date)
  1. scope       app/rag/scope.py       which wording version, endorsements and guides apply (rules, no model)
  2. rewrite     app/rag/answer.py      follow-ups become standalone questions (LLM, only when there is history)
  3. retrieve    app/rag/retriever.py   pgvector similarity + PostgreSQL full-text search, merged with RRF
     rerank      app/rag/rerank.py      a local cross-encoder re-scores the best 20 fused hits; top 6 are kept
  4. draft       app/rag/prompts.py     structured answer: claims, source ids, verbatim quotes (LLM)
  5. verify      app/rag/grounding.py   citations exist, quotes are verbatim, every number is in the cited
                                        text, no claim decisions, LLM judge agrees; failures are removed
  6. answer      summary + verified claims + citations, stored with the retrieval trace
```

The model layer is LangChain: `ChatOpenAI` (Responses API, structured output with strict JSON schemas,
`store=false`), `OpenAIEmbeddings`, `ChatPromptTemplate` chains, a `BaseRetriever` for the hybrid search and
`RecursiveCharacterTextSplitter` for chunking within policy sections. Reranking uses a small ONNX cross-encoder
(`Xenova/ms-marco-MiniLM-L-6-v2`) run locally with fastembed, so it needs no PyTorch and no extra API key; the
model is downloaded into the Docker image at build time. Ingestion (`app/ingestion/`) extracts
page text with pdfplumber, runs Tesseract OCR on scanned pages, removes running headers and footers, chunks
by section and stores page-accurate chunks with their embeddings.

Design choices:

- **PostgreSQL for everything** - documents, vectors (pgvector), full-text search, the ingestion job queue
  (`SELECT ... FOR UPDATE SKIP LOCKED`), sessions and audit. One system to run and back up; deleting a
  document removes it from search in the same transaction.
- **Our own chunk table rather than a vector-store library**, so access rules and version scoping are plain SQL
  filters with foreign keys and cascading deletes. Searches only ever run over the in-scope documents, so an
  exact vector scan is fast and no approximate index is needed.
- **Version selection in code, not in the prompt.** Answering from the wrong policy version is the most
  expensive mistake this tool can make, so it is decided deterministically and shown with every answer.

## Configuration

All settings are environment variables (see `.env.example` and `app/config.py`). The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_CHAT_MODEL` | `gpt-6-luna` | model for answers, follow-up rewriting and the grounding judge |
| `OPENAI_REASONING_EFFORT` | `low` | reasoning effort (`none`, `low`, `medium`, `high`) |
| `OPENAI_EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | `text-embedding-3-small` / `1536` | changing the dimension needs a migration and re-indexing |
| `GROUNDING_JUDGE` | `true` | LLM check of each statement (the deterministic checks always run) |
| `RETRIEVAL_TOP_K` | `6` | sources given to the model |
| `RETRIEVAL_MIN_SIMILARITY` | `0.25` | semantic matches below this are ignored |
| `RERANKER_ENABLED` | `true` | cross-encoder reranking of the fused search results |
| `RERANKER_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | any fastembed cross-encoder; rebuild with `--build-arg RERANKER_MODEL=...` |
| `RERANKER_CANDIDATES` | `20` | fused hits given to the reranker (the best `RETRIEVAL_TOP_K` are kept) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | set to your `https://` address behind a TLS proxy (enables secure cookies) |

## Tests and evaluation

```bash
docker compose run --rm tests                   # unit + integration tests (PostgreSQL, offline model fakes)
docker compose run --rm tests ruff check .      # lint
docker compose run --rm eval                    # golden-set evaluation against the real OpenAI models
```

The tests need no API key: `tests/fakes.py` provides offline LangChain-compatible stand-ins (hashed
bag-of-words embeddings and an extractive chat model), so the real prompts, retrieval SQL, verification and API
are exercised. The evaluation (`eval/`, see [eval/README.md](eval/README.md)) runs 71 hand-written questions
through the production pipeline and reports fact accuracy, retrieval and citation hit rates, version accuracy,
abstention and clarification behaviour, latency and cost. CI (`.github/workflows/ci.yml`) runs lint and tests
in Docker.

## Operations

- **Health:** `GET /health` (process up) and `GET /ready` (database, migrations, embedding model consistency,
  API key configured, reranker model loaded).
- **Logs:** JSON on stdout with a request id per request; API keys and passwords are redacted.
- **Usage:** the Admin page shows questions, active users, answer outcomes, latency, estimated model cost and
  feedback for the last 30 days, plus the audit log. `GET /api/admin/messages/{id}/trace` shows how an answer
  was produced (documents in scope, retrieval ranks and scores, removed claims).
- **Backups:** back up the `pgdata` and `uploads` volumes together, e.g.
  `docker compose exec db pg_dump -U postgres -Fc jigsaw_policy > jigsaw_policy.dump`.
- **Deployment:** run behind a TLS-terminating reverse proxy, set `APP_ENV=production` and an `https://`
  `PUBLIC_BASE_URL`. The API and worker run as a non-root user on a read-only filesystem, and the application
  connects to PostgreSQL as a non-superuser role (`docker/postgres/10-init.sh`).

## Security notes

- Passwords are hashed with Argon2id; accounts lock for 15 minutes after 5 failed sign-ins.
- Sessions are opaque random tokens (only their hash is stored) in `HttpOnly`, `SameSite=Strict` cookies, with
  a CSRF token for browser requests that change data. Scripts can use `POST /api/auth/token` and a bearer token.
- Document access is by role (`allowed_roles` on each document) and is enforced when choosing the documents
  for retrieval, so restricted text never reaches the model for users who cannot see it.
- Uploads are limited in size and pages, must be real PDFs, and are rejected if encrypted or if they contain
  JavaScript, automatic actions or attachments.
- Retrieved text is passed to the model as delimited, untrusted data; answers that contain claim decisions or
  instruction-like text are removed by the verification step.
- Before using real customer data, confirm the OpenAI data processing terms meet Jigsaw's requirements.

## Project layout

```
app/
  api/          FastAPI routers: auth, documents, chat, admin, health
  ingestion/    PDF validation, extraction and OCR, chunking, pipeline, worker
  rag/          scope, retriever, prompts, schemas, grounding, answer pipeline, LangChain model clients
  services/     document lifecycle, file storage, audit log
  web/          browser UI (plain HTML/CSS/ES modules, no build step)
  cli.py        migrate | worker | seed | create-user | ingest | retry-failed
migrations/     Alembic
sample_data/    Jigsaw product documents (Markdown sources, generated PDFs, manifest)
eval/           golden set, evaluation runner, reports
tests/          unit and integration tests, offline model fakes
docker/         PostgreSQL init script
```
