# PSA Card Scraper

A distributed web scraping system for collecting Pokemon trading card images from PSA (Professional Sports Authenticator) certification pages. The scraper automatically downloads, processes, and uploads card images to Google Cloud Storage, organized by PSA grade. **Note that written permission was received from PSA to carry out this scraping.** Data acquired from this distributed system will be used to build out deep learning models.

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

## License

This project is for educational and research purposes. Please respect PSA's terms of service and rate limits when using this scraper.

## Notes

- Certificate IDs are not sequential; gaps are common
- Chain processing optimizes for sequential cert IDs
- Multiple pods significantly increase throughput
- Regular cleanup recommended to maintain data quality
