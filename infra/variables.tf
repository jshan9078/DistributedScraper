variable "project_id" {}
variable "region" { default = "us-east1" }
variable "num_scrapers" { default = 3 }   # 1 node + 1 pod per scraper
variable "db_password" {}
variable "zone" {
  description = "Zone for the zonal GKE cluster"
  default     = "us-east1-b"
}