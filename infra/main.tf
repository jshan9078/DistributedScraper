terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ----------------- VPC -----------------
resource "google_compute_network" "vpc" {
  name                    = "psa-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  name          = "psa-subnet"
  ip_cidr_range = "10.0.0.0/16"
  region        = var.region
  network       = google_compute_network.vpc.id
}

# ----------------- Cloud SQL (Postgres) -----------------
resource "google_sql_database_instance" "psa" {
  name             = "psa-sql"
  region           = var.region
  database_version = "POSTGRES_17"
  settings {
    tier = "db-f1-micro"
    ip_configuration {
      ipv4_enabled = true     # public IPv4 (Cloud SQL proxy secures access)
    }
  }
}

resource "google_sql_database" "db" {
  name     = "psa"
  instance = google_sql_database_instance.psa.name
}

resource "google_sql_user" "user" {
  name     = "psa"
  instance = google_sql_database_instance.psa.name
  password = var.db_password
}

# ----------------- GCS Bucket -----------------
resource "google_storage_bucket" "dataset" {
  name                        = "psa-scan-scraping-dataset"
  location                    = var.region
  uniform_bucket_level_access = true
}

# ----------------- Artifact Registry -----------------
resource "google_artifact_registry_repository" "repo" {
  repository_id = "psa-repo"
  location      = var.region
  format        = "DOCKER"
}

# ----------------- IAM (Service Account for Pods) -----------------
resource "google_service_account" "scraper_sa" {
  account_id   = "scraper-sa"
  display_name = "Scraper Service Account"
}

resource "google_project_iam_member" "sa_roles" {
  for_each = toset([
    "roles/storage.objectAdmin",
    "roles/cloudsql.client",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.scraper_sa.email}"
}

# ----------------- GKE (Regional Cluster) -----------------
resource "google_container_cluster" "cluster" {
  name                     = "psa-cluster"
  location                 = var.zone    # <— changed from region to zone
  remove_default_node_pool = true
  initial_node_count       = 1
  network                  = google_compute_network.vpc.id
  subnetwork               = google_compute_subnetwork.subnet.id
  networking_mode          = "VPC_NATIVE"
  ip_allocation_policy {}
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # --- Important for ephemeral node IPs ---
  private_cluster_config {
    enable_private_nodes = false   # ensures each node gets its own public IP
  }
}

# ----------------- Node Pool (each node = unique public IP) -----------------
resource "google_container_node_pool" "scraper_pool" {
  name       = "scraper-pool"
  cluster    = google_container_cluster.cluster.name
  location   = var.zone
  node_count = var.num_scrapers

  node_config {
    machine_type    = "e2-medium"          # 4 vCPUs, 4 GB RAM
    disk_type       = "pd-standard"
    disk_size_gb    = 30
    preemptible     = false                # quota-safe; can enable later if approved
    service_account = google_service_account.scraper_sa.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    tags            = ["psa-scraper"]
    # When enable_private_nodes=false, each node gets an ephemeral public IP automatically.
  }

  autoscaling {
    min_node_count = 1
    max_node_count = 8
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}
