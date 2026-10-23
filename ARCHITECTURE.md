# Architecture

## 1. System

```
                         +------------------+
                         |     Browser      |
                         |  (HTML/JS in     |
                         |   app/web/)      |
                         +--------+---------+
                                  |  http://localhost:8000
                                  v
+----------------+      +------------------+      +------------------+
|    OpenAI      |<-----|       api        |      |     worker       |
|  chat model    |      |  FastAPI         |      |  ingests PDFs    |
|  embeddings    |<-----|  (app/api/)      |      |  (app/ingestion/)|
+----------------+      +--------+---------+      +--------+---------+
                                 |                         |
                                 v                         v
                        +--------------------------------------------+
                        |        PostgreSQL + pgvector  (db)         |
                        |  users, documents, chunks + vectors,       |
                        |  job queue, conversations, audit log       |
                        +--------------------------------------------+
```

All boxes except OpenAI run in Docker (`docker-compose.yml`). The `migrate` container sets up the tables
before `api` and `worker` start.

## 2. Adding a document

```
  PDF upload / seed
        |
        v
  validate + save file  -->  job queued
                                 |
                                 v
                          worker picks job
                                 |
                                 v
                    extract text  (OCR if scanned)
                                 |
                                 v
                     split into chunks by section
                                 |
                                 v
                    embed chunks  (OpenAI embeddings)
                                 |
                                 v
                      save to PostgreSQL  -->  status: ready
```

## 3. Answering a question

```
  Question
     |
     v
  1. Scope      pick product, region and policy version        (app/rag/scope.py)
     |          unclear? --> ask the user to choose
     v
  2. Search     vector search + keyword search, merged         (app/rag/retriever.py)
     |
     v
  3. Rerank     cross-encoder keeps the best 6 passages        (app/rag/rerank.py)
     |
     v
  4. Draft      OpenAI writes claims with quotes + sources     (app/rag/answer.py)
     |
     v
  5. Verify     check quotes, numbers and sources;             (app/rag/grounding.py)
     |          remove anything unsupported
     v
  Answer with page citations   (or "I can't answer that")
```

## 4. Folders

```
app/
  api/         web endpoints (login, documents, chat, admin)
  ingestion/   PDF -> text -> chunks -> vectors
  rag/         question -> search -> rerank -> answer -> verify
  services/    file storage, document lifecycle, audit
  web/         browser UI
migrations/    database schema
sample_data/   sample policy PDFs
tests/         automated tests
eval/          answer-quality evaluation
```
