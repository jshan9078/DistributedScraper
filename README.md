# Distributed Web Scraping Framework

A production-grade distributed web scraping system built on Google Kubernetes Engine (GKE) with PostgreSQL-based work queue coordination. Originally designed for collecting Pokemon trading card images from PSA certification pages, this framework provides a **reusable architecture for any large-scale web scraping project** requiring distributed coordination, fault tolerance, and horizontal scalability.

**Current Implementation**: PSA card image scraper with automated download, processing, and cloud storage. **Note that written permission was received from PSA to carry out this scraping.** Data acquired from this distributed system will be used to build out deep learning models.

## Key Technologies

**Infrastructure & Orchestration**
- Google Kubernetes Engine (GKE)
- Terraform
- Docker
- Kubernetes

**Cloud Services (GCP)**
- Cloud SQL (PostgreSQL 17)
- Google Cloud Storage (GCS)
- Artifact Registry
- VPC Networking

**Web Scraping**
- Selenium WebDriver
- BeautifulSoup
- Headless Chrome/Chromium

**Data Processing & Storage**
- Pillow (PIL)
- psycopg2

**Development**
- Python 3.10
- kubectl
- gcloud CLI

## Purpose

This project scrapes PSA-certified Pokemon card images for dataset creation and analysis. It:
- Extracts high-resolution images of card fronts and backs
- Crops and processes images to remove PSA certification holders
- Organizes cards by their PSA grade (1-10)
- Stores processed images in Google Cloud Storage
- Supports distributed scraping with multiple workers to avoid duplication

## Architecture

### Core Components

#### 1. **Scraper Engine** ([scraper.py](scraper.py))
The main scraping orchestrator with the following architecture:

**Database Layer**
- **PostgreSQL Work Queue**: Coordinates work between distributed pods
  - `work_queue` table tracks cert status: `pending`, `in_progress`, `done`, `error`, `stale`, `skipped`
  - Atomic operations with `FOR UPDATE SKIP LOCKED` prevent race conditions
  - Worker-specific offsets prevent overlap between pods

**Scraping Strategy**
- **Queue Mode**: Processes pending certificates from the work queue
- **Exploration Mode**: Randomly explores certificate ID space when queue is empty
- **Chain Processing**: Sequentially processes adjacent cert IDs to maximize efficiency

**Web Automation**
- Uses Selenium with headless Chrome for JavaScript-rendered pages
- Implements stealth techniques to avoid bot detection
- Waits for dynamic image loading before extraction
- Rate limiting and error handling with exponential backoff

**Image Processing Pipeline**
1. Download high-resolution images from PSA servers
2. Parse and upgrade `/small/` URLs to `/large/` versions
3. Crop card area from PSA holder using fixed reference coordinates
4. Convert to optimized PNG format
5. Upload to GCS organized by grade: `png/{grade}/{cert_id}_{side}.png`

**Key Features**
- Multi-pod coordination via CloudSQL
- Duplicate prevention with worker-specific offsets
- Configurable exploration bounds (cert ID ranges)
- Automatic chain breaking on non-Pokemon/Japanese cards
- Graceful error recovery with retry logic

#### 2. **Cleanup Utility** ([cleanup_incomplete_certs.py](cleanup_incomplete_certs.py))
Maintains data quality by:
- Scanning GCS bucket for incomplete certificates
- Identifying certs with only front OR back (not both)
- Deleting incomplete entries to ensure dataset consistency

#### 3. **Statistics Tool** ([count_cards_by_grade.py](count_cards_by_grade.py))
Provides dataset analytics:
- Counts complete certificates per PSA grade
- Validates data integrity (both sides present)
- Detects certificates in multiple grade folders
- Generates summary reports

### Data Flow

```
Certificate ID → Selenium Fetch → HTML Parsing → Image Download
                                                        ↓
GCS Upload ← PNG Optimization ← Crop Card ← Load Image
```

### Work Queue State Machine

```
pending → in_progress → done
                     → error (retry after cooldown)
                     → stale (page load failed)
                     → skipped (non-Pokemon/Japanese)
```

## Configuration

### Environment Variables

Required environment variables (typically set in Kubernetes deployment):

```bash
DB_HOST          # PostgreSQL host address
DB_USER          # Database username (default: psa)
DB_PASSWORD      # Database password
DB_NAME          # Database name (default: psa)
GCS_BUCKET       # Google Cloud Storage bucket name
HOSTNAME         # Worker ID (auto-set by Kubernetes)
```

### Google Cloud Authentication

Set credentials path:
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
```

### Scraper Parameters

Key configuration constants in [scraper.py](scraper.py):

- `MAX_IMAGES`: Maximum images to scrape per run (default: 10000)
- `CERT_MIN/CERT_MAX`: Certificate ID exploration bounds
- `WAIT_MIN/WAIT_MAX`: Random delay between requests (20-30s)
- `RATE_LIMIT_COOLDOWN`: Cooldown after errors (600s)
- `CONSECUTIVE_ERRORS_THRESHOLD`: Max errors before cooldown (3)

## Setup

### Prerequisites

- Python 3.9+
- Chrome/Chromium browser
- ChromeDriver matching your Chrome version
- PostgreSQL database with `work_queue` table
- Google Cloud Storage bucket
- GCS service account with storage write permissions

### Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set up database schema:
```sql
CREATE TABLE work_queue (
    cert_id BIGINT PRIMARY KEY,
    status VARCHAR(20) NOT NULL,
    worker_id VARCHAR(100),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_status ON work_queue(status);
CREATE INDEX idx_cert_id ON work_queue(cert_id);
```

3. Configure environment variables (see Configuration section)

### Usage

**Run the main scraper:**
```bash
python scraper.py
```

**Clean incomplete certificates:**
```bash
python cleanup_incomplete_certs.py
```

**Generate statistics:**
```bash
python count_cards_by_grade.py
```

## Deployment

This project uses a complete Infrastructure-as-Code approach with **Terraform**, **Docker**, and **Kubernetes (GKE)** for distributed scraping at scale.

### Infrastructure Overview

```
Terraform → GKE Cluster → Node Pool → Pods → Docker Containers → scraper.py
    ↓           ↓            ↓          ↓
  Cloud SQL   VPC/Subnet   Autoscaling  Workload Identity
  GCS Bucket
  Artifact Registry
```

### Architecture Layers

#### 1. **Terraform Infrastructure** ([infra/](infra/))

Terraform provisions all Google Cloud resources:

**Networking**
- VPC network (`psa-vpc`) with custom subnet (10.0.0.0/16)
- Public IP per node for anti-bot detection diversity

**Data Layer**
- **Cloud SQL**: PostgreSQL 17 instance (`db-f1-micro`) for work queue coordination
- **GCS Bucket**: `psa-scan-scraping-dataset` for image storage
- **Artifact Registry**: Docker image repository in us-east1

**Compute - GKE Cluster**
- **Cluster Type**: Zonal cluster in `us-east1-b` (cost-optimized)
- **Workload Identity**: Enabled for secure pod → GCP service authentication
- **Networking Mode**: VPC-native for optimal pod networking

**Node Pool Configuration** ([infra/main.tf:104-130](infra/main.tf#L104-L130))
- **Machine Type**: `e2-medium` (2 vCPUs, 4GB RAM per node)
- **Disk**: 30GB standard persistent disk
- **Service Account**: `scraper-sa` with roles:
  - `roles/storage.objectAdmin` (GCS write access)
  - `roles/cloudsql.client` (CloudSQL connection)

**Autoscaling Configuration**
```hcl
autoscaling {
  min_node_count = 1
  max_node_count = 8
}
```
- **Min Nodes**: 1 (cost savings during idle)
- **Max Nodes**: 8 (scales based on CPU/memory metrics)
- **Auto-repair**: Enabled (replaces unhealthy nodes)
- **Auto-upgrade**: Enabled (keeps GKE version current)

**Key Terraform Files**
- [infra/main.tf](infra/main.tf) - Resource definitions
- [infra/variables.tf](infra/variables.tf) - Configurable parameters
- [infra/outputs.tf](infra/outputs.tf) - Connection strings and endpoints

**Deploy Infrastructure**
```bash
cd infra
terraform init
terraform plan -var="project_id=your-project" -var="db_password=secure-password"
terraform apply
```

#### 2. **Docker Container** ([Dockerfile](Dockerfile))

Containerizes the scraper with all dependencies:

**Base Image**: `python:3.10-slim`

**Key Components**
- **Chromium + ChromeDriver**: Pre-installed for headless scraping
- **Python Dependencies**: Selenium, BeautifulSoup, Pillow, psycopg2, google-cloud-storage
- **Fonts & Libraries**: Liberation fonts, NSS, GTK for proper rendering

**Build & Push**
```bash
# Build image
docker build -t us-east1-docker.pkg.dev/PROJECT_ID/psa-repo/scraper:latest .

# Authenticate to Artifact Registry
gcloud auth configure-docker us-east1-docker.pkg.dev

# Push to registry
docker push us-east1-docker.pkg.dev/PROJECT_ID/psa-repo/scraper:latest
```

**Environment Variables**
- `PYTHONUNBUFFERED=1`: Live log streaming
- `CHROME_BIN=/usr/bin/chromium`: Chrome binary path
- `CHROMEDRIVER_PATH=/usr/bin/chromedriver`: Driver path

#### 3. **Kubernetes Deployment** ([k8s-deployment.yaml](k8s-deployment.yaml))

Orchestrates multiple scraper pods with coordination and fault tolerance.

**Deployment Spec**
```yaml
kind: Deployment
replicas: 8  # Run 8 concurrent scraper pods
```

**Pod Configuration**

*Container Specs*
- **Image**: `us-east1-docker.pkg.dev/psa-scan-scraping/psa-repo/scraper:latest`
- **Pull Policy**: `Always` (ensures latest version)

*Resource Limits* ([k8s-deployment.yaml:47-53](k8s-deployment.yaml#L47-L53))
```yaml
requests:
  cpu: "300m"      # 0.3 CPU cores minimum
  memory: "512Mi"  # 512MB minimum
limits:
  cpu: "600m"      # 0.6 CPU cores max
  memory: "1Gi"    # 1GB max
```

*Environment Variables*
- **Database Credentials**: Injected from `db-credentials` secret
- **GCS Bucket**: `psa-scan-scraping-dataset`
- **Cert Range**: MIN_CERT_ID=70000000, MAX_CERT_ID=120000000
- **Pod Identity**: `POD_IP` for unique worker identification

*Volume Mounts*
- **GCS Credentials**: `/var/secrets/google/key.json` (service account key)
- **Shared Memory**: `/dev/shm` (prevents Chrome crashes)

**Health Checks**

*Startup Probe* ([k8s-deployment.yaml:70-79](k8s-deployment.yaml#L70-L79))
- Checks if Python process started
- 30 failures × 10s = 5 min grace period
- Prevents premature pod restarts during Chrome init

*Readiness Probe* ([k8s-deployment.yaml:56-67](k8s-deployment.yaml#L56-L67))
- Verifies Python running + CloudSQL connection (port 5432)
- 20s initial delay, 15s intervals
- Ensures pod only receives traffic when fully ready

**Secrets Required**
```bash
# Database credentials
kubectl create secret generic db-credentials \
  --from-literal=DB_HOST=127.0.0.1 \
  --from-literal=DB_USER=psa \
  --from-literal=DB_PASSWORD=your-password \
  --from-literal=DB_NAME=psa

# Service account key for GCS
kubectl create secret generic scraper-service-account-key \
  --from-file=key.json=/path/to/service-account-key.json
```

**Deploy to Kubernetes**
```bash
# Get cluster credentials
gcloud container clusters get-credentials psa-cluster --zone=us-east1-b

# Apply deployment
kubectl apply -f k8s-deployment.yaml

# Check pod status
kubectl get pods -l app=psa-scraper

# View logs
kubectl logs -l app=psa-scraper --tail=100 -f
```

### Autoscaling Behavior

**Node-Level Autoscaling** (Terraform-managed)
1. **Scale Up**: When pod resource requests exceed available node capacity
   - Example: 8 pods × 512Mi = 4GB > available node memory
   - GKE provisions new nodes (up to max_node_count=8)
2. **Scale Down**: When nodes are underutilized for 10+ minutes
   - GKE drains and deletes nodes (down to min_node_count=1)

**Pod-Level Scaling** (Manual via replicas)
- Current: Fixed 8 replicas
- Can add HPA (Horizontal Pod Autoscaler) based on:
  - CPU utilization
  - Memory usage
  - Custom metrics (queue depth from CloudSQL)

**Cost Optimization**
- Minimum 1 node when idle (~$25/month)
- Scales to 8 nodes during peak scraping
- Preemptible nodes disabled (quota-safe, can enable for 80% savings)

### Coordination & Race Prevention

**Work Queue Atomicity** ([scraper.py:89-112](scraper.py#L89-L112))
```sql
FOR UPDATE SKIP LOCKED  -- PostgreSQL row-level locking
```
- Only one pod can claim each cert_id
- No duplicate scraping between pods

**Worker-Specific Offsets** ([scraper.py:478](scraper.py#L478))
```python
worker_offset = hash(WORKER_ID) % 1000
```
- Each pod explores different cert_id ranges
- Reduces collision during exploration mode

**Chain Breaking** ([scraper.py:404-406](scraper.py#L404-L406))
- When non-Pokemon/Japanese card detected, chain stops
- Pod returns to queue mode, picks up new work
- Prevents wasted sequential processing

### Monitoring & Debugging

**View Pod Logs**
```bash
# All pods
kubectl logs -l app=psa-scraper --tail=100 -f

# Specific pod
kubectl logs psa-scraper-xyz123 -f
```

**Check Resource Usage**
```bash
kubectl top pods -l app=psa-scraper
kubectl top nodes
```

**Exec into Pod**
```bash
kubectl exec -it psa-scraper-xyz123 -- /bin/bash
```

**Check Work Queue**
```bash
# From any pod with psql access
kubectl exec -it psa-scraper-xyz123 -- psql $DB_CONN_STRING -c \
  "SELECT status, COUNT(*) FROM work_queue GROUP BY status;"
```

## Data Organization

GCS bucket structure:
```
bucket-name/
├── png/
│   ├── 1/          # PSA Grade 1
│   ├── 2/          # PSA Grade 2
│   ├── ...
│   ├── 10/         # PSA Grade 10 (Gem Mint)
│   └── unknown/    # Grade not detected
│       ├── {cert_id}_front.png
│       └── {cert_id}_back.png
```

## Error Handling

- **Rate Limiting**: 10-minute cooldown after 3 consecutive errors
- **Stale Pages**: Marked and skipped if page doesn't load
- **Missing Images**: Chain breaks, returns to queue mode
- **Network Errors**: Retry with exponential backoff
- **Database Conflicts**: Atomic operations prevent duplicate work

## Limitations

- Only scrapes English Pokemon cards
- Skips Japanese/Asian variants
- Requires Chrome/Chromium installation
- Rate limited to respect PSA servers
- Sequential processing per chain (parallel chains via multiple pods)

## Adapting for Other Scraping Projects

This framework can be repurposed for any large-scale web scraping project. The architecture separates concerns into modular components that can be adapted independently.

### Core Reusable Components

#### 1. **Work Queue Coordination System**
The PostgreSQL-based work queue with atomic operations (`FOR UPDATE SKIP LOCKED`) is **domain-agnostic** and can coordinate any distributed task.

**Adaptation Steps:**
1. Modify the `work_queue` table schema to match your domain:
   ```sql
   CREATE TABLE work_queue (
       task_id VARCHAR(255) PRIMARY KEY,  -- Your unique identifier (URL, product ID, etc.)
       status VARCHAR(20) NOT NULL,
       worker_id VARCHAR(100),
       priority INT DEFAULT 0,            -- Optional: priority-based processing
       metadata JSONB,                    -- Optional: store task-specific data
       updated_at TIMESTAMP DEFAULT NOW()
   );
   ```

2. Update `fetch_next_cert()` in [scraper.py:89-112](scraper.py#L89-L112) to use your task identifier
3. Modify `insert_new_cert()` to handle your task format

**Use Cases:**
- E-commerce product scraping (task_id = product URL)
- Social media data collection (task_id = user ID or post ID)
- Document archival (task_id = document URL)
- API pagination (task_id = page offset or cursor)

#### 2. **Dual-Mode Processing Strategy**

The queue + exploration pattern works for any scenario where:
- You have a **known set of tasks** (queue mode)
- You want to **discover new tasks** (exploration mode)

**Examples:**
- **Job Board Scraper**: Queue processes known job postings, exploration discovers new listings via search
- **Real Estate Scraper**: Queue processes known property IDs, exploration crawls category pages for new listings
- **Academic Paper Scraper**: Queue processes known DOIs, exploration follows citation graphs

**Adaptation:**
- Modify `process_chain()` logic to match your discovery pattern
- Update exploration logic ([scraper.py:478-479](scraper.py#L478-L479)) to generate candidates relevant to your domain

#### 3. **Chain Processing Optimization**

Sequential processing works when your task space has **locality** (adjacent IDs likely both valid).

**Applicable Domains:**
- Sequential numeric IDs (invoices, orders, certificates)
- Timestamp-based iteration (recent posts, daily archives)
- Alphabetically sorted resources (dictionary entries, SKUs)

**Adaptation:**
- Replace cert_id increment logic with your sequencing strategy
- Define chain-breaking conditions for your domain (e.g., HTTP 404, category change)

#### 4. **Kubernetes Infrastructure**

The GKE + Terraform setup is **completely domain-independent**. No changes needed to:
- [infra/main.tf](infra/main.tf) - Infrastructure provisioning
- [k8s-deployment.yaml](k8s-deployment.yaml) - Pod orchestration (except environment variables)
- [Dockerfile](Dockerfile) - Container build (may need different dependencies)

**Only Change:**
- Environment variables (database name, bucket name, task-specific configs)
- Container dependencies if using different scraping libraries

### Example Adaptations

#### Example 1: E-commerce Product Scraper

**Changes Required:**
1. **Work Queue**: task_id = product URL
2. **Scraper Logic**:
   - Replace `fetch_psa_page()` with product page fetching
   - Replace image cropping with product data extraction (price, description, reviews)
   - Replace GCS upload with database insert or CSV export
3. **Chain Processing**: Follow pagination links or "similar products"
4. **Exploration**: Start from category pages, discover new products

**Files to Modify:**
- [scraper.py](scraper.py): Lines 200-446 (scraping logic)
- [k8s-deployment.yaml](k8s-deployment.yaml): Lines 36-45 (environment variables)

#### Example 2: Social Media Archive

**Changes Required:**
1. **Work Queue**: task_id = user ID or post ID
2. **Scraper Logic**:
   - Replace Selenium with API calls (if available) or HTML parsing
   - Store posts/comments in structured format (JSON to GCS or PostgreSQL)
3. **Chain Processing**: Follow user timelines or comment threads
4. **Exploration**: Discover new users via followers/following

**Files to Modify:**
- [scraper.py](scraper.py): Lines 150-500 (entire scraping pipeline)
- [Dockerfile](Dockerfile): May not need Chromium if using API

#### Example 3: Document Archive/Research Database

**Changes Required:**
1. **Work Queue**: task_id = document URL or DOI
2. **Scraper Logic**:
   - Download PDFs/documents instead of images
   - Extract metadata (author, date, abstract)
   - Store in GCS with organized folder structure
3. **Chain Processing**: Follow citation graphs or reference lists
4. **Exploration**: Crawl search result pages or journal archives

**Files to Modify:**
- [scraper.py](scraper.py): Lines 200-300 (download + processing logic)
- [requirements.txt](requirements.txt): Add PDF parsing libraries (PyPDF2, pdfplumber)

### Minimal Changes Checklist

To adapt this framework for a new domain:

- [ ] **Database**: Update `work_queue` schema for your task identifier
- [ ] **Scraper Core**: Replace PSA-specific logic (fetch, parse, process)
- [ ] **Storage**: Modify upload logic for your data format (GCS, CloudSQL, filesystem)
- [ ] **Exploration**: Define how new tasks are discovered
- [ ] **Chain Logic**: Decide if sequential processing applies to your domain
- [ ] **Environment Variables**: Update [k8s-deployment.yaml](k8s-deployment.yaml) with your configs
- [ ] **Dependencies**: Update [requirements.txt](requirements.txt) and [Dockerfile](Dockerfile) as needed

### What Stays the Same

You **do not need to modify**:
- PostgreSQL atomic operations (work queue coordination)
- Kubernetes deployment structure (pod management, health checks)
- Terraform infrastructure (VPC, GKE cluster, node autoscaling)
- Worker offset collision avoidance
- Error handling and rate limiting patterns
- Distributed coordination logic

### Benefits of This Architecture

**Horizontal Scalability**: Add more pods to increase throughput linearly
- 8 pods = 8x throughput
- 64 pods = 64x throughput (with more nodes)

**Fault Tolerance**:
- Pods can crash without losing work (state in PostgreSQL)
- Automatic pod restarts via Kubernetes
- Node auto-repair and auto-upgrade

**Zero Race Conditions**:
- Atomic PostgreSQL operations guarantee no duplicate work
- Multiple pods can safely claim tasks concurrently

**Cost Efficiency**:
- Auto-scales from 1 node (idle) to 8 nodes (active scraping)
- Pay only for what you use

**Production-Ready**:
- Health checks prevent cascading failures
- Resource limits prevent runaway processes
- Workload identity for secure authentication
- Infrastructure-as-code for reproducibility

## License

This project is for educational and research purposes. Please respect target website terms of service and rate limits when using this scraper.

## Notes

- Certificate IDs are not sequential; gaps are common
- Chain processing optimizes for sequential cert IDs
- Multiple pods significantly increase throughput
- Regular cleanup recommended to maintain data quality
