# Proposed AWS design: unmesh regular vs medical, no ALB stickiness

This replaces a single AWS ALB (sticky sessions) in front of the current
QuickScribe + Medical monolith. The current Site (`QS_Site` / `siteapp.py`)
already behaves like a sticky single-node service, even on Koyeb.

## Why the current shape is the problem

| Constraint in production today | Effect |
|---|---|
| `gunicorn --workers 1` | Cannot scale the Site horizontally |
| Socket.IO **long-polling only** (no WebSocket) | Engine.IO `sid` is pinned to one process |
| Live medical ASR is an in-process AWS Transcribe bridge keyed by that `sid` | Audio must keep hitting the same worker |
| `pending_trigger`, `job_results_cache`, SageMaker warmup hints are in-memory | Cross-worker requests look like lost jobs |
| Regular `/api/check_status` polls share the same gevent loop as medical ASR | Consumer traffic can stall clinic live transcription |

A sticky ALB cookie is just that same design moved to AWS. It fails for the
usual reasons: target replacement drops sessions, scale-out does not help
live ASR, PHI and anonymous consumer traffic share one blast radius, and
hospital clients already cannot use WebSockets so cookie affinity becomes
load-balancer policy instead of an application protocol.

Regular and medical are also **meshed in one app**:

- One Flask process, one Socket.IO server, one `is_medical` flag through
  sign-s3 / trigger / callback / GPT / billing
- Consumer storage is Cloudflare R2 + RunPod; medical is KMS S3 + SageMaker
  + AWS Transcribe — but both are invoked from the same IAM principal
- Frontend medical affinity is a sticky localStorage/path overlay on the
  same SPA as `/` and `/en`

## Target shape

Two products, two networks, two data planes. Live ASR is a third *medical*
plane so HTTP APIs never need stickiness.

```text
                    ┌─────────────────────────────────────────┐
 Consumer           │  getquickscribe.com                     │
 (no PHI)           │  CloudFront → Koyeb (or ECS later)      │
                    │  R2 + RunPod GPU/CPU                    │
                    │  Supabase consumer project              │
                    │  Stateless HTTP + DB-backed job state   │
                    └─────────────────────────────────────────┘

                    ┌─────────────────────────────────────────┐
 Medical HTTP       │  medical.getquickscribe.com             │
 (PHI, HIPAA acct)  │  ALB  stickiness = off                  │
                    │  ECS Fargate n≥2                        │
                    │  RDS/Redis + S3/KMS + SageMaker async   │
                    │  SQS for callbacks and scale events     │
                    └──────────────────┬──────────────────────┘
                                       │ session_id only
                    ┌──────────────────▼──────────────────────┐
 Medical live ASR   │  stream.medical.getquickscribe.com      │
                    │  ALB or API Gateway HTTP (no cookies)   │
                    │  Redis stream + leased Transcribe owner │
                    │  AWS Transcribe Streaming (eu-west-1)   │
                    └─────────────────────────────────────────┘
```

Do **not** put consumer and medical on one ALB, one target group, or one
IAM role.

## 1. Unmesh the products

### Account and network

- **Consumer AWS (optional):** only if you later leave Koyeb. No BAA, no PHI,
  no access to medical buckets or SageMaker.
- **Medical AWS account:** BAA, dedicated VPC, CloudTrail, KMS CMK, private
  SageMaker, no shared credentials with the consumer Site.
- DNS: `www.getquickscribe.com` stays consumer. Medical is
  `medical.getquickscribe.com` (or a clinic subdomain). No path-based
  `/medical` on the consumer origin.

### Application split

| Surface | Consumer service | Medical service |
|---|---|---|
| Auth / credits | Anonymous + paid minutes | Medical SaaS accounts, Cardcom |
| Upload | Presign R2 | Presign KMS S3 (`raw-audio/…`) |
| File ASR | RunPod `/run` + GPU callback | SageMaker async + SQS completion |
| Live ASR | None | Dedicated streaming plane |
| Jobs DB | Consumer Supabase | Medical RDS or a **separate** Supabase project |
| Frontend | Current SPA minus medical chrome | Medical-only UI, no consumer credit UX |

Delete `is_medical` from the consumer codebase after the split. Shared
libraries (docx export, prompt helpers) can be a package; they must not
share process, Redis, or IAM.

### Data split

Keep the existing storage split, make it *enforced*:

- Consumer: `S3_BUCKET` / R2 `users/{id}/input|output`
- Medical: `MEDICAL_S3_BUCKET` + KMS `raw-audio/`, `transcripts/`, `summaries/`
- Bucket policies and task roles so consumer tasks cannot `s3:*` medical
  and medical tasks cannot read R2
- Separate log groups; never log transcript text or PCM

## 2. Remove ALB stickiness

Stickiness exists only because **session ownership lives in a process**.
Move ownership to Redis (or DynamoDB) and ALB can be round-robin.

### Medical HTTP API — fully stateless

ALB → ECS, `stickiness.enabled = false`.

Persist what is in-memory today:

| Today (process dict) | After |
|---|---|
| `pending_trigger` / `pending_trigger_at` | Job row (`queued` / `run_accepted` / `triggered` / `failed`) |
| `pending_job_info`, credit reserve | Same job row |
| `job_results_cache` | Transcript object in S3 + job row pointer |
| SageMaker warmup / desired capacity | EventBridge → **SQS** (not SNS HTTPS to one worker). Status API always `DescribeEndpoint` + `DescribeScalableTargets` (already the source of truth) |
| Socket.IO `job_status_update` rooms | Client polls `GET /jobs/{id}` or SSE from any task |

SageMaker async is already the right file-ASR pattern: submit, callback
later, any API task can accept the callback **if** the job row is in the
database. Replace `POST /api/gpu_callback` affinity assumptions with
idempotent writes keyed by `job_id`.

SNS HTTPS to Site is the other sticky leftover: it updates an in-memory
hint on whichever task got the POST. Use EventBridge → SQS → any ECS
task, or skip the hint entirely and let `GET /api/medical_endpoint_status`
keep reading AWS (current poll path is already correct).

### Medical live ASR — lease, do not sticky-route

Hospital/corporate proxies already forced Engine.IO **polling** instead of
WebSockets. Cookie stickiness is the load-balancer version of that
workaround. Replace it with an explicit session:

```text
1. Client POST /asr/sessions  → { session_id, upload_url or chunk_path }
2. Client POST /asr/sessions/{id}/audio  (PCM chunks, any ECS task)
      task XADD redis asr:{id}  → PCM frames
3. One worker holds a Redis lease asr-owner:{id}
      that worker alone keeps the AWS Transcribe Streaming connection
4. Partials go to Redis pub/sub or a stream asr-out:{id}
5. Client GET /asr/sessions/{id}/events  (SSE or poll) from any task
6. Client POST /asr/sessions/{id}/complete → medical HTTP API writes
      transcript + billing (existing complete_stream_transcription flow)
```

Properties:

- ALB has **no** stickiness; any task accepts audio or poll
- TCP affinity to AWS Transcribe stays inside the leased worker, not in
  the load balancer
- Scale-out adds API tasks immediately; ASR owners are a separate pool
- Task death: lease expires, a new owner starts a fresh Transcribe
  stream (brief gap, no sticky cookie outage)
- Regular consumer HTTP never shares this gevent loop

Do not put this on an NLB+WebSocket unless hospital networks are re-tested.
The current polling constraint is real; the Redis-lease design preserves
HTTP polling without ALB cookies.

Consumer Socket.IO for job progress can die with the split: polling
`trigger_status` / `check_status` is already the UI handshake. If you
keep Socket.IO on consumer, give it a Redis message queue — still no
ALB stickiness, and **do not** share that Redis with medical.

## 3. Compute mapping (keep what already works)

| Workload | Keep | Where it runs |
|---|---|---|
| Consumer file ASR | RunPod GPU Whisper | Unchanged, called only by consumer Site |
| Consumer music / loudnorm | RunPod CPU | Unchanged |
| Medical file ASR | SageMaker async endpoint | Medical account only |
| Medical live ASR | AWS Transcribe Streaming (`eu-west-1`) | Leased ASR workers |
| Medical endpoint 0↔1 | Application Auto Scaling + EventBridge | SQS, not Site in-memory |
| Email | SES | Split identities if needed (marketing vs clinic) |

Do not send medical audio to RunPod. Do not send consumer audio to the
HIPAA account.

## 4. Frontend unmesh

Today medical mode is a sticky overlay on the same JS bundle (`app_logic.js`
path/localStorage affinity, plus live Transcribe client). After the split:

- Consumer origin never loads `qs_aws_transcribe_stream.js`
- Medical origin never shows anonymous credit / music / vocal-separation
- Sign-in to medical does not write consumer “sticky medical” keys
- Shared marketing pages can link across sites; sessions do not

## 5. What not to build

- One ALB, two target groups, path `/medical` vs `/` — still a mesh
  (shared WAF, shared cert, shared logs, accidental routing)
- ALB stickiness “just for Socket.IO” — encodes the single-worker
  Procfile into AWS
- Redis as a *shared* bus between consumer and medical
- Putting Engine.IO WebSocket behind CloudFront (already rejected)
- Using the consumer `AWS_REGION=auto` (R2) credentials for Transcribe
  or SageMaker

## 6. Migration (no big-bang)

1. **Extract medical HTTP + ASR into a second process** (still on Koyeb
   if you want). Consumer Site loses `is_medical` routes. Proves unmesh
   without an AWS move. Regular poll storms can no longer stall live ASR.
2. **Externalize job state** (Supabase/RDS) until gunicorn can run
   `workers > 1` with stickiness still off.
3. **Live ASR Redis lease** on the medical process; delete Engine.IO
   sid affinity.
4. **Move medical process to ECS** in the HIPAA account: ALB (no
   stickiness) + SQS + KMS S3 + SageMaker. Consumer stays on Koyeb.
5. **Cut DNS** `medical.getquickscribe.com`, remove `/medical` from the
   consumer app, split IAM.

Phase 1–3 already remove the need for ALB stickiness. Phase 4 is the AWS
design. Phase 5 is the product split users see.

## Success criteria

- Medical ALB target group: stickiness disabled, ≥2 healthy tasks
- Kill one medical API task mid-upload: job continues from DB
- Kill one ASR owner mid-session: new lease, stream resumes without
  a sticky cookie
- Consumer load test does not change medical live-ASR latency
- Consumer IAM cannot read `MEDICAL_S3_BUCKET`; medical IAM cannot
  call RunPod or R2
- No `is_medical` branch left in the consumer Site
